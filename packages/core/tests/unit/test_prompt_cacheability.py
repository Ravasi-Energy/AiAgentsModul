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


def test_specialist_path_has_no_hardcoded_marker() -> None:
    """base.py must not hand-roll a cache_control dict — it routes through
    build_cacheable_system so the threshold is measured per prompt and model.
    committee_reviewers sends bare (its prompts are ~230-350 tokens)."""
    from openexecutive.agents import base
    from openexecutive.orchestrator import committee_reviewers

    assert _cache_control_keys(base) == 0
    assert _cache_control_keys(committee_reviewers) == 0


# ---- cacheability is measured, not asserted in a comment -------------------

def test_short_prompt_sends_bare() -> None:
    from openexecutive.prompts.cache_manager import build_cacheable_system

    assert build_cacheable_system(CSO_PROMPT, "claude-opus-5") == CSO_PROMPT


def test_prompt_over_the_threshold_is_cached() -> None:
    """TALENT_PROMPT is ~1087 tokens — above the Sonnet/Opus minimum. A blanket
    'domain prompts are too short' rule would have silently stopped caching it."""
    from openexecutive.prompts.cache_manager import build_cacheable_system
    from openexecutive.prompts.domain_prompts import TALENT_PROMPT

    block = build_cacheable_system(TALENT_PROMPT, "claude-opus-5")
    assert isinstance(block, list)
    assert block[0]["cache_control"] == {"type": "ephemeral"}


def test_haiku_needs_double_the_prefix() -> None:
    """Haiku's minimum is 2048, so a prompt that caches on Opus may not here."""
    from openexecutive.prompts.cache_manager import build_cacheable_system
    from openexecutive.prompts.domain_prompts import TALENT_PROMPT

    assert isinstance(build_cacheable_system(TALENT_PROMPT, "claude-opus-5"), list)
    assert build_cacheable_system(TALENT_PROMPT, "claude-haiku-4-5") == TALENT_PROMPT


def test_tool_definitions_count_toward_the_prefix() -> None:
    """Tools sit AHEAD of the system block in the cached prefix, so they push a
    short prompt over the minimum. The tools path and the prose path therefore
    reach different answers for the same prompt — which is why the earlier
    blanket comment (copied to both) was wrong."""
    from openexecutive.prompts.cache_manager import build_cacheable_system

    assert build_cacheable_system(CSO_PROMPT, "claude-opus-5") == CSO_PROMPT
    with_tools = build_cacheable_system(
        CSO_PROMPT, "claude-opus-5", prefix_tokens=1000
    )
    assert isinstance(with_tools, list)


def test_long_operator_override_regains_caching() -> None:
    """effective_system_prompt() can return an operator override of any length.
    A hardcoded 'too short' rule would strand a 5k-token custom prompt with no
    caching and no way to re-enable it."""
    from openexecutive.prompts.cache_manager import build_cacheable_system

    override = "You are a custom executive. " * 400
    assert isinstance(build_cacheable_system(override, "claude-opus-5"), list)


def test_ttl_is_only_emitted_when_requested() -> None:
    from openexecutive.prompts.cache_manager import build_cacheable_system
    from openexecutive.prompts.triage_prompt import TRIAGE_PROMPT

    default = build_cacheable_system(TRIAGE_PROMPT, "claude-haiku-4-5")
    assert isinstance(default, list)
    assert "ttl" not in default[0]["cache_control"]

    hour = build_cacheable_system(TRIAGE_PROMPT, "claude-haiku-4-5", ttl="1h")
    assert isinstance(hour, list)
    assert hour[0]["cache_control"]["ttl"] == "1h"
