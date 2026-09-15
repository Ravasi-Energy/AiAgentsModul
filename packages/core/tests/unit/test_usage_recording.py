"""Every model call site records a ``cache_event`` usage row: specialist
consults, specialist tool calls, triage, the chat memory extractor, the
committee reviewers, and the per-session utility calls (the Executive's own
turns and the research loops are covered by their module tests).

``test_usage_instrumentation_coverage.py`` enforces that no call site is
missing; these tests assert the rows carry the right actor and model."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openexecutive.agents.base import BaseAgent
from openexecutive.audit.logger import AuditLogger, set_audit_logger


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    logger = AuditLogger(tmp_path / "audit.db")
    set_audit_logger(logger)
    yield logger  # type: ignore[misc]
    set_audit_logger(None)


def _response(**usage: object) -> SimpleNamespace:
    return SimpleNamespace(
        content=[], stop_reason="end_turn", usage=SimpleNamespace(**usage),
    )


class _Agent(BaseAgent):
    name = "unit_agent"
    domain = "unit"
    model = "claude-test"

    def get_system_prompt(self) -> str:
        return "prompt"


def test_analyze_with_tools_records_usage_under_the_callers_actor(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=_response(
        input_tokens=70, output_tokens=9,
        server_tool_use=SimpleNamespace(web_search_requests=3),
    )))
    monkeypatch.setattr("openexecutive.agents.base.get_provider", lambda _m: provider)

    asyncio.run(_Agent().analyze_with_tools(
        "ctx", tools=[{"name": "t", "input_schema": {"type": "object"}}],
        model_override="claude-research", actor="specialist_research",
    ))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1
    assert rows[0].actor == "specialist_research"
    assert rows[0].details["model"] == "claude-research"
    assert rows[0].details["web_search_requests"] == 3


def test_analyze_records_usage_as_specialist(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat-turn consult (``consult_specialist`` → ``analyze``) writes one
    usage row under the default ``specialist`` actor, so the session cost
    summary and ``/audit/usage`` count it."""
    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    response = _response(input_tokens=120, output_tokens=40, cache_read_input_tokens=100)
    response.content = [SimpleNamespace(type="text", text="analysis")]
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=response))
    monkeypatch.setattr("openexecutive.agents.base.get_provider", lambda _m: provider)

    text = asyncio.run(_Agent().analyze("What should we do?"))

    assert text == "analysis"
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1
    assert rows[0].actor == "specialist"
    assert rows[0].details["model"] == "claude-test"
    assert rows[0].details["input_tokens"] == 120
    assert rows[0].details["output_tokens"] == 40
    assert rows[0].details["cache_read_input_tokens"] == 100


def test_analyze_records_usage_under_an_explicit_actor(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Council test box passes ``actor="agent_test"`` so sandbox runs
    are separable from production consults in the by-source breakdown."""
    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    response = _response(input_tokens=5, output_tokens=1)
    response.content = [SimpleNamespace(type="text", text="ok")]
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=response))
    monkeypatch.setattr("openexecutive.agents.base.get_provider", lambda _m: provider)

    asyncio.run(_Agent().analyze("q", actor="agent_test"))

    rows = audit.query(event_type="cache_event")
    assert [r.actor for r in rows] == ["agent_test"]


def test_route_to_specialist_tags_rows_with_the_active_turn(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct router call (a workflow step) records under the
    ``specialist_workflow`` actor and inherits the active audit turn, which
    is what lets ``/audit/sessions/{id}`` group the row under its turn."""
    from openexecutive.audit.context import set_turn
    from openexecutive.orchestrator import router

    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    monkeypatch.setitem(router.SPECIALIST_REGISTRY, "unit_agent", _Agent())
    response = _response(input_tokens=8, output_tokens=3)
    response.content = [SimpleNamespace(type="text", text="ok")]
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=response))
    monkeypatch.setattr("openexecutive.agents.base.get_provider", lambda _m: provider)

    with set_turn(session_id="s-1", turn_id="t-1"):
        asyncio.run(router.route_to_specialist("unit_agent", "q"))

    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1
    assert rows[0].actor == "specialist_workflow"
    assert rows[0].session_id == "s-1"
    assert rows[0].turn_id == "t-1"


def test_executive_test_box_records_usage(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Council's Executive preview is a raw provider call; it records
    under ``agent_test`` like the specialist test box."""
    from openexecutive.api.routes.agents import AgentTestRequest, _test_executive

    monkeypatch.setattr("openexecutive.api.routes.agents.get_override", lambda _name: None)
    response = _response(input_tokens=9, output_tokens=2)
    response.content = [SimpleNamespace(type="text", text="preview")]
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=response))
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: provider)

    text = asyncio.run(_test_executive(AgentTestRequest(query="hi", prompt="P", model="claude-x")))

    assert text == "preview"
    rows = audit.query(event_type="cache_event")
    assert [r.actor for r in rows] == ["agent_test"]
    assert rows[0].details["model"] == "claude-x"


def test_triage_records_usage(audit: AuditLogger) -> None:
    from openexecutive.agents.triage import TriageAgent
    from openexecutive.alerts.models import AlertEvent

    block = SimpleNamespace(type="tool_use", name="emit_alert_decision", input={
        "alert": False, "severity": "low", "channels": ["persisted"], "headline": "h",
        "body": "b", "suggested_action": "", "topic_tags": [], "dedup_key": "k",
        "reason_if_suppressed": "noise",
    })
    response = SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=33, output_tokens=5))
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    asyncio.run(TriageAgent().triage(
        AlertEvent(source="email", external_id="m1", subject="s", body="b"), client=client,
    ))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1 and rows[0].actor == "triage"
    assert rows[0].details["input_tokens"] == 33


def test_memory_extractor_records_usage(
    audit: AuditLogger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory import episodic as ep

    db = tmp_path / "episodic.db"
    ep.initialize_db(db)
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=_response(
        input_tokens=12, output_tokens=2,
    )))
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: provider)
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: SimpleNamespace(routing_model="claude-test"),
    )
    asyncio.run(ep.extract_and_store("user message", "assistant response", db_path=db))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1 and rows[0].actor == "memory_extractor"
    assert rows[0].details["model"] == "claude-test"


def _text_response(text: str, **usage: object) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(**usage),
    )


def test_session_title_records_usage_under_its_own_actor(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-session utility call is cheap but not free — it must be visible."""
    from openexecutive.utils import session_title

    provider = SimpleNamespace(messages_create=AsyncMock(
        return_value=_text_response("Pricing strategy", input_tokens=40, output_tokens=4)
    ))
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: provider)
    monkeypatch.setattr(
        "openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-fast"
    )

    title = asyncio.run(session_title.generate_session_title("hi", "hello"))

    assert title == "Pricing strategy"
    rows = audit.query(event_type="cache_event")
    assert [r.actor for r in rows] == ["session_title"]
    assert rows[0].details["model"] == "claude-fast"
    assert rows[0].details["input_tokens"] == 40


def test_committee_reviewer_records_usage_per_critique(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committee review triples the model calls on a turn; each one is a row."""
    from openexecutive.orchestrator.committee_reviewers import Reviewer

    provider = SimpleNamespace(messages_create=AsyncMock(
        return_value=_text_response(
            '{"severity": "low", "issues": [], "suggestions": []}',
            input_tokens=120, output_tokens=15,
        )
    ))
    monkeypatch.setattr(
        "openexecutive.orchestrator.committee_reviewers.get_provider",
        lambda _m: provider,
    )

    reviewer = Reviewer(name="quality", system_prompt="judge", model="claude-review")
    asyncio.run(reviewer.critique("question", "draft"))

    rows = audit.query(event_type="cache_event")
    assert [r.actor for r in rows] == ["committee_reviewer"]
    assert rows[0].details["model"] == "claude-review"


def test_anthropic_provider_pins_max_retries() -> None:
    """A retried call is billed but only the final response reports usage, so
    the retry count governs how far /audit/usage can undercount. Pinning it
    keeps that knob reviewable; the default matches the SDK, so behavior is
    unchanged."""
    import anthropic

    from openexecutive.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(api_key="sk-test", max_retries=0)
    assert provider._client.max_retries == 0

    # Omitted -> the SDK's own default still applies (no behavior change).
    # Compare against the SDK rather than pinning a literal, so an upstream
    # bump is not a spurious failure in this repo.
    sdk_default = anthropic.AsyncAnthropic(api_key="sk-test").max_retries
    assert AnthropicProvider(api_key="sk-test")._client.max_retries == sdk_default
