"""BO-BOT-002: typed predicate evaluator — three-valued logic tests."""
from __future__ import annotations

import pytest

from openexecutive.bo.bots.predicates import (
    MAX_DEPTH,
    PredicateError,
    Tri,
    evaluate,
)

CTX = {
    "service": {"name": "billing", "last_heartbeat_age_minutes": 45},
    "severity": "warning",
    "count": 3,
    "flag": True,
    "nothing": None,
}


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"op": "eq", "path": "severity", "value": "warning"}, Tri.TRUE),
        ({"op": "eq", "path": "severity", "value": "info"}, Tri.FALSE),
        ({"op": "ne", "path": "severity", "value": "info"}, Tri.TRUE),
        ({"op": "in", "path": "severity",
          "values": ["info", "warning"]}, Tri.TRUE),
        ({"op": "in", "path": "severity", "values": ["info"]}, Tri.FALSE),
        ({"op": "exists", "path": "service.name"}, Tri.TRUE),
        ({"op": "exists", "path": "service.missing"}, Tri.FALSE),
        ({"op": "exists", "path": "nothing"}, Tri.TRUE),  # present-and-null exists
        ({"op": "gt", "path": "service.last_heartbeat_age_minutes",
          "value": 30}, Tri.TRUE),
        ({"op": "gt", "path": "service.last_heartbeat_age_minutes",
          "value": 60}, Tri.FALSE),
        ({"op": "gte", "path": "count", "value": 3}, Tri.TRUE),
        ({"op": "lt", "path": "count", "value": 3}, Tri.FALSE),
        ({"op": "lte", "path": "count", "value": 3}, Tri.TRUE),
        # strict typing: "5" != 5
        ({"op": "eq", "path": "count", "value": "3"}, Tri.FALSE),
    ],
)
def test_leaf_ops(node: dict, expected: Tri) -> None:
    assert evaluate(node, CTX) is expected


@pytest.mark.parametrize(
    "node",
    [
        {"op": "eq", "path": "absent", "value": 1},
        {"op": "ne", "path": "absent", "value": 1},
        {"op": "in", "path": "absent", "values": [1, 2]},
        {"op": "gt", "path": "absent", "value": 1},
        {"op": "lt", "path": "deep.absent", "value": 1},
        # typed comparison against a non-number is UNKNOWN, not an error
        {"op": "gt", "path": "severity", "value": 1},
        {"op": "lt", "path": "flag", "value": 1},
        {"op": "gte", "path": "service.name", "value": 0},
    ],
)
def test_missing_or_untyped_is_unknown(node: dict) -> None:
    """BO-BOT-003: missing input evaluates to UNKNOWN — never coerced."""
    assert evaluate(node, CTX) is Tri.UNKNOWN


def test_all_any_not_three_valued() -> None:
    t = {"op": "eq", "path": "severity", "value": "warning"}
    f = {"op": "eq", "path": "severity", "value": "info"}
    u = {"op": "eq", "path": "absent", "value": 1}

    assert evaluate({"all": [t, t]}, CTX) is Tri.TRUE
    assert evaluate({"all": [t, f]}, CTX) is Tri.FALSE
    assert evaluate({"all": [t, u]}, CTX) is Tri.UNKNOWN   # UNKNOWN propagates
    assert evaluate({"all": [u, u]}, CTX) is Tri.UNKNOWN
    assert evaluate({"all": [f, u]}, CTX) is Tri.FALSE    # FALSE dominates

    assert evaluate({"any": [f, f]}, CTX) is Tri.FALSE
    assert evaluate({"any": [f, t]}, CTX) is Tri.TRUE
    assert evaluate({"any": [f, u]}, CTX) is Tri.UNKNOWN
    assert evaluate({"any": [u, u]}, CTX) is Tri.UNKNOWN

    assert evaluate({"not": t}, CTX) is Tri.FALSE
    assert evaluate({"not": f}, CTX) is Tri.TRUE
    assert evaluate({"not": u}, CTX) is Tri.UNKNOWN       # NOT UNKNOWN = UNKNOWN


def test_tri_is_not_truthy() -> None:
    with pytest.raises(TypeError):
        bool(Tri.UNKNOWN)
    with pytest.raises(TypeError):
        bool(Tri.TRUE)


@pytest.mark.parametrize(
    "node",
    [
        {"op": "wat", "path": "a", "value": 1},
        {"op": "eq", "value": 1},                        # path missing
        {"op": "eq", "path": "", "value": 1},
        {"op": "eq", "path": "a..b", "value": 1},
        {"op": "exists", "path": "a", "value": 1},       # exists takes no value
        {"op": "in", "path": "a", "values": []},
        {"op": "in", "path": "a"},                       # values missing
        {"op": "gt", "path": "a", "value": "x"},         # numeric op needs number
        {"op": "eq", "path": "a", "value": 1, "sneaky": True},
        {"all": []},
        {"all": [{"op": "eq", "path": "a", "value": 1}] * 21},
        {"all": [{"op": "eq", "path": "a", "value": 1}], "extra": 1},
        "not-an-object",
    ],
)
def test_validation_rejects_bad_nodes(node: object) -> None:
    with pytest.raises(PredicateError):
        evaluate(node, CTX)  # type: ignore[arg-type]


def test_depth_limit() -> None:
    node: dict = {"op": "eq", "path": "a", "value": 1}
    for _ in range(MAX_DEPTH):
        node = {"not": node}
    with pytest.raises(PredicateError):
        evaluate(node, CTX)


def test_no_eval_or_shell_surface() -> None:
    """The grammar is closed: nothing in a predicate can smuggle code."""
    with pytest.raises(PredicateError):
        evaluate({"op": "eq", "path": "a", "value": 1,
                  "exec": "__import__('os')"}, CTX)
    with pytest.raises(PredicateError):
        evaluate({"op": "eval", "path": "a"}, CTX)
