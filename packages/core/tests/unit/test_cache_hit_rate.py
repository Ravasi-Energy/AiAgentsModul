"""`cache_hit_rate` on the usage views.

docs/architecture.md has long advertised a 70-85% prompt-cache hit rate with
nothing in the codebase computing it, so nobody could tell whether caching was
working. The counters were already logged; this exposes the ratio on every
usage rollup (totals, by-day, by-model, by-source).
"""
from __future__ import annotations

import pytest

from openexecutive.api.routes.audit import TokenCounts, UsageTotals


def _counts(**kw: int) -> TokenCounts:
    base = {
        "calls": 1,
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
    }
    return TokenCounts(**{**base, **kw})


def test_all_reads_is_a_perfect_rate() -> None:
    assert _counts(cache_read_input_tokens=1000).cache_hit_rate == 1.0


def test_all_fresh_input_is_zero() -> None:
    assert _counts(input_tokens=1000).cache_hit_rate == 0.0


def test_reads_are_measured_against_total_billed_input() -> None:
    rate = _counts(input_tokens=200, cache_read_input_tokens=800).cache_hit_rate
    assert rate == 0.8


def test_cache_writes_count_against_the_rate() -> None:
    """A workload that writes a cache it never reads is not healthy, and must
    not score as though the write were free — a write costs MORE than fresh
    input, so excluding it from the denominator would flatter exactly the
    failure mode this number exists to reveal."""
    rate = _counts(
        cache_read_input_tokens=800, cache_creation_input_tokens=200
    ).cache_hit_rate
    assert rate == 0.8

    # The pathological case: constant writes, zero reads.
    assert _counts(cache_creation_input_tokens=5000).cache_hit_rate == 0.0


def test_empty_window_does_not_divide_by_zero() -> None:
    assert _counts().cache_hit_rate == 0.0


def test_output_tokens_are_excluded() -> None:
    """Output is not prompt input and cannot be cached; including it would
    make the rate drift with response length."""
    quiet = _counts(cache_read_input_tokens=100, output_tokens=0)
    chatty = _counts(cache_read_input_tokens=100, output_tokens=100_000)
    assert quiet.cache_hit_rate == chatty.cache_hit_rate == 1.0


def test_rate_is_serialised_on_the_api_models() -> None:
    """It must reach the response body — a property that Pydantic does not
    emit would leave the /audit/usage page exactly as blind as before."""
    totals = UsageTotals(
        calls=2,
        input_tokens=100,
        cache_read_input_tokens=300,
        cache_creation_input_tokens=0,
        output_tokens=50,
        cost_usd=0.01,
    )
    dumped = totals.model_dump()
    assert dumped["cache_hit_rate"] == pytest.approx(0.75)
