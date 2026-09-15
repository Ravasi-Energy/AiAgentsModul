"""Committee review on inbound email follows EMAIL_COMMITTEE_REVIEW.

It used to be hardcoded on, justified purely on latency ("the +5-12s does not
matter on a 60s poll cycle") with no weighing of cost: committee adds three
reviewer calls plus a full revision pass to EVERY inbound email, on a path any
sender can trigger since unrostered senders are deliberately not dropped.

The quality argument is real — a recipient reads the reply with no chance to
refine it — so the setting stays available. It is just a deployment's call
rather than a default, and these tests pin both directions.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import openexecutive.integrations.email_poller as poller


class _CapturingExecutive:
    def __init__(self, **_kwargs: Any) -> None:
        self.chat_kwargs: dict[str, Any] | None = None

    async def chat(self, **kwargs: Any) -> str:
        self.chat_kwargs = kwargs
        return "ok"


def _settings(committee: bool = False) -> Any:
    return SimpleNamespace(
        exec_email_address="exec@example.com",
        email_poll_interval_seconds=60,
        email_committee_review=committee,
    )


def _run(committee: bool) -> dict[str, Any]:
    """Drive one inbound email and return the kwargs handed to Executive.chat."""
    captured: list[_CapturingExecutive] = []

    def _factory(**kwargs: Any) -> _CapturingExecutive:
        ex = _CapturingExecutive(**kwargs)
        captured.append(ex)
        return ex

    with (
        patch("openexecutive.orchestrator.executive.Executive", new=_factory),
        patch(
            "openexecutive.onboarding.profile_builder.load_or_create_profile",
            return_value=SimpleNamespace(is_empty=lambda: True),
        ),
        patch(
            "openexecutive.knowledge.retriever.retrieve",
            new=lambda **_k: "",
        ),
        patch(
            "openexecutive.memory.episodic.format_for_prompt",
            new=lambda: "",
        ),
        patch.object(poller, "get_settings", return_value=_settings(committee)),
    ):
        gateway = AsyncMock()
        asyncio.run(
            poller._run_executive(
                gateway=gateway,
                raw_email="Subject: test\nFrom: alice@example.com\n\nbody",
                message_id="m1",
                thread_id="t1",
                from_addr="alice@example.com",
            )
        )

    assert len(captured) == 1
    kwargs = captured[0].chat_kwargs
    assert kwargs is not None
    return kwargs


def test_committee_review_is_off_by_default() -> None:
    """The expensive path must not be the default on a surface any sender can
    reach — committee is 3 reviewer calls plus a revision on every email."""
    assert _run(committee=False).get("committee_review") is False


def test_committee_review_is_honoured_when_enabled() -> None:
    """With the setting on, the reply Gmail sees is the reviewed revision
    rather than the raw draft."""
    assert _run(committee=True).get("committee_review") is True
