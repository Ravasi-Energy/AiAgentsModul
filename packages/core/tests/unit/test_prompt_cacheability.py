"""Prompts carrying ``cache_control`` must clear the model's minimum.

Anthropic ignores a cache breakpoint on a prefix shorter than the minimum
cacheable length — silently. No error is raised, nothing is cached, and the
block bills as ordinary input on every call. A marker on a short prompt
therefore *looks* like caching without being it, which is how the specialist
prompts carried dead markers for a long time.

These tests pin the two halves of that: prompts we deliberately cache must stay
long enough to actually cache, and prompts we deliberately do NOT cache must
not regrow a marker without someone re-checking the threshold.
"""
from __future__ import annotations

import pytest

from openexecutive.prompts.cache_manager import (
    MIN_CACHEABLE_TOKENS_HAIKU,
    MIN_CACHEABLE_TOKENS_SONNET_OPUS,
    estimate_tokens,
)
from openexecutive.prompts.domain_prompts import (
    BOARD_COMMS_PROMPT,
    CFO_PROMPT,
    CSO_PROMPT,
)
from openexecutive.prompts.triage_prompt import TRIAGE_PROMPT


def test_triage_prompt_clears_the_haiku_minimum() -> None:
    """Triage runs on the routing model (Haiku by default) once per monitoring
    event, so its breakpoint is worth having — but only while the prompt stays
    above Haiku's higher threshold. If a future edit trims it below this, the
    marker becomes a silent no-op and this test is the warning."""
    assert estimate_tokens(TRIAGE_PROMPT) >= MIN_CACHEABLE_TOKENS_HAIKU


def _cache_control_keys(module: object) -> int:
    """Count real ``cache_control`` dict keys, ignoring prose in comments."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and key.value == "cache_control"
    )


def test_triage_call_marks_its_system_block() -> None:
    from openexecutive.agents import triage

    assert _cache_control_keys(triage) == 1


@pytest.mark.parametrize(
    "prompt", [CSO_PROMPT, CFO_PROMPT, BOARD_COMMS_PROMPT]
)
def test_domain_prompts_are_below_the_threshold_and_uncached(prompt: str) -> None:
    """Documents *why* specialist prompts carry no marker. If a domain prompt
    ever grows past the minimum, caching it becomes worthwhile and this test
    should fail so someone revisits that decision deliberately."""
    assert estimate_tokens(prompt) < MIN_CACHEABLE_TOKENS_SONNET_OPUS


def test_specialist_calls_send_a_bare_system_prompt() -> None:
    """A marker must not creep back onto the specialist path without the
    prompt first growing past the threshold above."""
    from openexecutive.agents import base
    from openexecutive.orchestrator import committee_reviewers

    assert _cache_control_keys(base) == 0
    assert _cache_control_keys(committee_reviewers) == 0
