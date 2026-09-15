"""The assembled Executive request must stay within Anthropic's 4 breakpoints.

``cache_control`` markers are capped at 4 per request across tools, system and
messages combined. The Executive already spends all four — the tool block, two
system blocks, and the rolling penultimate-assistant-turn marker — so there is
no headroom. A fifth marker added anywhere is an API 400 at runtime, on every
chat turn, which is a production outage rather than a test failure.

This test assembles a real request through ``stream_chat`` (worst case: a
company profile present so the second system block exists, and enough history
for the rolling marker) and counts what actually goes on the wire.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.session import Session

# Anthropic's hard limit on cache_control blocks in one request.
MAX_CACHE_BREAKPOINTS = 4


class _Stream:
    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def get_final_message(self) -> Any:
        # Shape the loop actually consumes: content blocks, a terminal
        # stop_reason so it does not iterate again, and a usage block.
        return SimpleNamespace(
            content=[],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=2),
        )


def _count_markers(obj: Any) -> int:
    """Every ``cache_control`` key anywhere in the request payload."""
    if isinstance(obj, dict):
        return sum(
            (1 if k == "cache_control" else 0) + _count_markers(v)
            for k, v in obj.items()
        )
    if isinstance(obj, list):
        return sum(_count_markers(item) for item in obj)
    return 0


@pytest.fixture()
def captured_request() -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Provider:
        def messages_stream(self, **kwargs: Any) -> _Stream:
            captured.update(kwargs)
            return _Stream()

    session = Session(session_id="cache-budget")
    # Enough history that the rolling penultimate-turn marker applies — that
    # marker is the fourth breakpoint and only exists once history is deep
    # enough, so a shallower session would under-count.
    session.add_user_message("first question")
    session.add_assistant_message("first answer")
    session.add_user_message("second question")
    session.add_assistant_message("second answer")

    async def _go() -> None:
        async for _ in Executive().stream_chat(
            user_message="and now a third", session=session
        ):
            pass

    with patch(
        "openexecutive.orchestrator.executive.get_provider",
        return_value=_Provider(),
    ):
        asyncio.run(_go())

    assert captured, "provider was never called — the harness did not exercise a turn"
    return captured


def test_request_stays_within_the_breakpoint_budget(
    captured_request: dict[str, Any],
) -> None:
    total = (
        _count_markers(captured_request.get("system"))
        + _count_markers(captured_request.get("tools"))
        + _count_markers(captured_request.get("messages"))
    )
    assert total <= MAX_CACHE_BREAKPOINTS, (
        f"The assembled chat request carries {total} cache_control markers; "
        f"Anthropic allows {MAX_CACHE_BREAKPOINTS}. A fifth is a hard 400 on "
        "every turn. Remove a marker, or merge two blocks that change on the "
        "same cadence (build_system_blocks already does this for the company "
        "profile and org context)."
    )


def test_the_budget_is_actually_being_used(
    captured_request: dict[str, Any],
) -> None:
    """Guard the guard: if the assembly stops emitting markers entirely,
    the ceiling test above would pass while caching silently died."""
    total = (
        _count_markers(captured_request.get("system"))
        + _count_markers(captured_request.get("tools"))
        + _count_markers(captured_request.get("messages"))
    )
    assert total >= 2, (
        f"Only {total} cache_control markers in the assembled request — the "
        "Executive should cache at least its persona block and its tool block. "
        "Prompt caching may have regressed."
    )


def test_exactly_one_marker_on_the_tool_block(
    captured_request: dict[str, Any],
) -> None:
    """Tools are a single cached prefix: the marker belongs on the last tool
    only. One per tool would blow the budget instantly."""
    assert _count_markers(captured_request.get("tools")) == 1


# ---- rolling conversation cache -------------------------------------------

def test_rolling_cache_marker_actually_fires(
    captured_request: dict[str, Any],
) -> None:
    """Regression: this marker was dead.

    It targeted ``len(history) - 2`` while also requiring an assistant role.
    _build_messages runs before the current user message is appended, so
    history is always even-length and ends on an assistant turn — which makes
    ``len(history) - 2`` a USER index at every depth. The two conditions could
    never both hold, so the conversation prefix was re-sent uncached on every
    turn of every session.
    """
    messages = captured_request.get("messages") or []
    marked = [m for m in messages if _count_markers(m)]
    assert len(marked) == 1, (
        "Expected exactly one rolling cache marker on the conversation "
        f"history, found {len(marked)}."
    )


def test_rolling_cache_marker_lands_on_the_last_assistant_turn(
    captured_request: dict[str, Any],
) -> None:
    """It must mark the end of the stable prefix. Marking anything earlier
    leaves later history uncached; marking the current user turn would move
    the breakpoint every request and never hit."""
    messages = captured_request.get("messages") or []
    marked_indexes = [i for i, m in enumerate(messages) if _count_markers(m)]
    assert len(marked_indexes) == 1

    idx = marked_indexes[0]
    assert messages[idx]["role"] == "assistant"
    # Everything after it must be the current turn's user message only.
    assert all(m["role"] == "user" for m in messages[idx + 1 :])


def test_marker_is_absent_when_caching_is_disabled() -> None:
    """ENABLE_CACHING=false must switch the marker off, not just shift it."""
    captured: dict[str, Any] = {}

    class _Provider:
        def messages_stream(self, **kwargs: Any) -> _Stream:
            captured.update(kwargs)
            return _Stream()

    session = Session(session_id="cache-disabled")
    session.add_user_message("q1")
    session.add_assistant_message("a1")

    executive = Executive()
    with (
        patch.object(executive._settings, "enable_caching", False),
        patch(
            "openexecutive.orchestrator.executive.get_provider",
            return_value=_Provider(),
        ),
    ):

        async def _go() -> None:
            async for _ in executive.stream_chat(user_message="q2", session=session):
                pass

        asyncio.run(_go())

    assert _count_markers(captured.get("messages")) == 0


# ---- the marker must stand down when it cannot possibly hit ----------------

def _drive(session: Session, message: str = "next") -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Provider:
        def messages_stream(self, **kwargs: Any) -> _Stream:
            captured.update(kwargs)
            return _Stream()

    with patch(
        "openexecutive.orchestrator.executive.get_provider",
        return_value=_Provider(),
    ):

        async def _go() -> None:
            async for _ in Executive().stream_chat(
                user_message=message, session=session
            ):
                pass

        asyncio.run(_go())
    return captured


def test_no_marker_once_the_history_window_starts_sliding() -> None:
    """Regression: re-activating the rolling marker made long sessions WORSE.

    get_recent_history keeps the last MAX_HISTORY_TURNS exchanges. Past that the
    window slides — the oldest exchange is dropped every turn, so messages[0]
    changes and the cached prefix can never match. Marking it then buys a
    guaranteed cache WRITE (billed above plain input) with no possible read, on
    exactly the long sessions that cost the most. The marker must stand down.
    """
    from openexecutive.orchestrator.session import MAX_HISTORY_TURNS

    session = Session(session_id="long")
    for i in range(MAX_HISTORY_TURNS + 3):
        session.add_user_message(f"u{i}")
        session.add_assistant_message(f"a{i}")

    assert not session.history_window_is_stable()
    assert _count_markers(_drive(session).get("messages")) == 0


def test_marker_present_while_the_window_is_still_stable() -> None:
    """The flip side: a short session must still get the rolling cache."""
    session = Session(session_id="short")
    session.add_user_message("u0")
    session.add_assistant_message("a0")

    assert session.history_window_is_stable()
    assert _count_markers(_drive(session).get("messages")) == 1


def test_marker_walks_back_past_structured_assistant_content() -> None:
    """Session.add_assistant_message accepts list content. Pinning the marker
    to the last assistant turn would then mark nothing at all; walking back to
    the newest string turn degrades to partial caching instead of none."""
    session = Session(session_id="structured")
    session.add_user_message("u0")
    session.add_assistant_message("a0-plain")
    session.add_user_message("u1")
    session.add_assistant_message([{"type": "text", "text": "a1-structured"}])

    messages = _drive(session).get("messages") or []
    marked = [m for m in messages if _count_markers(m)]
    assert len(marked) == 1
    assert marked[0]["content"][0]["text"] == "a0-plain"
