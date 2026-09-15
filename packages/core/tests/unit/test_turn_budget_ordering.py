"""A turn must not both run the research council and fan out to the cap.

`run_executive_research` runs a whole specialist council synchronously inside
the agent loop, so it is charged against the per-turn budget at its real
weight. But the charge and the specialist partition live in the SAME iteration
body: charging after the partition let the FIRST iteration dispatch a full-width
fan-out and *then* run the council, which is precisely what the budget exists to
prevent. The ceiling only bit from iteration 2 onward.

This pins the ordering by capturing the `remaining_budget` the partition
actually sees.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.research_tools import research_turn_budget_weight
from openexecutive.orchestrator.session import Session


def _tool_use(name: str, tid: str, payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, id=tid, input=payload)


class _Stream:
    """Yields one assistant turn containing research + three consults."""

    def __init__(self, blocks: list[Any]) -> None:
        self._blocks = blocks

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def get_final_message(self) -> Any:
        return SimpleNamespace(
            content=self._blocks,
            stop_reason="tool_use" if self._blocks else "end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


@pytest.fixture()
def observed_budgets() -> list[int | None]:
    """Drive one turn whose first assistant message asks for research AND a
    wide specialist fan-out, and record what the partition was told."""
    seen: list[int | None] = []
    real = __import__(
        "openexecutive.orchestrator.router", fromlist=["partition_specialist_fanout"]
    ).partition_specialist_fanout

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("remaining_budget"))
        return real(*args, **kwargs)

    first = [
        _tool_use("run_executive_research", "r1", {"note": ""}),
        *[
            _tool_use("consult_specialist", f"s{i}", {"specialist": "cso", "query": "q"})
            for i in range(3)
        ],
    ]
    streams = iter([_Stream(first), _Stream([])])

    class _Provider:
        def messages_stream(self, **_kw: Any) -> _Stream:
            return next(streams)

    session = Session(session_id="budget-order")

    with (
        patch("openexecutive.orchestrator.executive.get_provider", return_value=_Provider()),
        patch("openexecutive.orchestrator.executive.partition_specialist_fanout", new=_spy),
        patch.dict(
            "openexecutive.orchestrator.executive._ALL_SKILL_HANDLERS",
            {"run_executive_research": AsyncMock(return_value='{"ok": true}')},
        ),
        patch(
            "openexecutive.orchestrator.executive.route_parallel",
            # One analysis per DISPATCHED call — route_parallel returns results
            # 1:1 with the calls it was given, and the loop zips them strictly.
            new=AsyncMock(side_effect=lambda calls, **_kw: ["analysis"] * len(calls)),
        ),
    ):

        async def _go() -> None:
            async for _ in Executive().stream_chat(
                user_message="research the market, then ask the CSO", session=session
            ):
                pass

        asyncio.run(_go())

    assert seen, "partition_specialist_fanout was never reached"
    return seen


def test_research_is_charged_before_the_specialist_partition(
    observed_budgets: list[int | None],
) -> None:
    from openexecutive.config import get_settings

    budget = get_settings().max_specialist_calls_per_turn
    first_seen = observed_budgets[0]
    assert first_seen is not None

    # If the charge landed first, the partition sees the budget already reduced
    # by the council's weight. If it landed after, it would see the full budget.
    assert first_seen == budget - research_turn_budget_weight(), (
        f"partition saw remaining_budget={first_seen} on the first iteration; "
        f"expected {budget - research_turn_budget_weight()} (full budget "
        f"{budget} minus the research weight {research_turn_budget_weight()}). "
        "The research charge is running after the partition, so the first "
        "iteration can research AND fan out to the cap."
    )
    assert first_seen < budget
