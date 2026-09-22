"""Typed predicate evaluator for BoBots — three-valued logic.

Operators (``bo.bobot.v1``):
    leaf:   {"op": "eq|ne|in|exists|lt|lte|gt|gte", "path": "a.b.c", ...}
    groups: {"all": [...]}  {"any": [...]}  {"not": {...}}

Semantics that matter (BO-BOT-002):
- A missing input is ``UNKNOWN``, never a coerced ``false`` — and UNKNOWN in
  a gate does not let the effect through.
- ``exists`` is the only operator that always decides: the value is present
  or it is not.
- Comparisons are typed: a numeric operator on a non-number is UNKNOWN, not
  an error and not ``False``.
- No eval, no SQL, no shell — the node grammar below is the whole language.
  Anything else fails validation at save/publish time.

Resource limits keep a hostile or sloppy predicate cheap: depth ≤ 6,
nodes ≤ 64, list literals ≤ 100 items, strings ≤ 1000 chars.
"""
from __future__ import annotations

import enum
from typing import Any

MAX_DEPTH = 6
MAX_NODES = 64
MAX_LIST_ITEMS = 100
MAX_STRING = 1000
MAX_PATH_SEGMENT = 64


class Tri(enum.Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"

    def __bool__(self) -> bool:  # pragma: no cover - guard rail
        raise TypeError("Tri is three-valued; use == Tri.TRUE/FALSE/UNKNOWN")


class PredicateError(ValueError):
    """Structural validation failure — the predicate is malformed."""


_MISSING = object()

_OPS = ("eq", "ne", "in", "exists", "lt", "lte", "gt", "gte")


def _resolve(context: Any, path: str) -> Any:
    """Walk ``a.b.c`` through dicts. Returns _MISSING when absent."""
    node = context
    for seg in path.split("."):
        if isinstance(node, dict) and seg in node:
            node = node[seg]
        else:
            return _MISSING
    return node


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_leaf(op: str, value: Any, node: dict[str, Any]) -> Tri:
    if op == "exists":
        return Tri.FALSE if value is _MISSING else Tri.TRUE
    if value is _MISSING:
        return Tri.UNKNOWN

    if op == "eq":
        expected = node["value"]
        # Strict: "5" != 5. Dict/list equality is allowed (structural).
        return Tri.TRUE if value == expected else Tri.FALSE
    if op == "ne":
        return Tri.FALSE if value == node["value"] else Tri.TRUE
    if op == "in":
        return Tri.TRUE if value in node["values"] else Tri.FALSE

    # Numeric comparisons — typed, so a non-number on either side is UNKNOWN.
    expected = node["value"]
    if not (_is_number(value) and _is_number(expected)):
        return Tri.UNKNOWN
    if op == "lt":
        return Tri.TRUE if value < expected else Tri.FALSE
    if op == "lte":
        return Tri.TRUE if value <= expected else Tri.FALSE
    if op == "gt":
        return Tri.TRUE if value > expected else Tri.FALSE
    return Tri.TRUE if value >= expected else Tri.FALSE  # gte


def evaluate(node: dict[str, Any], context: dict[str, Any]) -> Tri:
    """Validate + evaluate one predicate tree against ``context``."""
    _validate(node, depth=1, budget=[MAX_NODES])
    return _eval(node, context)


def _eval(node: dict[str, Any], context: dict[str, Any]) -> Tri:
    if "all" in node:
        seen_unknown = False
        for child in node["all"]:
            r = _eval(child, context)
            if r is Tri.FALSE:
                return Tri.FALSE
            seen_unknown = seen_unknown or r is Tri.UNKNOWN
        return Tri.UNKNOWN if seen_unknown else Tri.TRUE
    if "any" in node:
        seen_unknown = False
        for child in node["any"]:
            r = _eval(child, context)
            if r is Tri.TRUE:
                return Tri.TRUE
            seen_unknown = seen_unknown or r is Tri.UNKNOWN
        return Tri.UNKNOWN if seen_unknown else Tri.FALSE
    if "not" in node:
        r = _eval(node["not"], context)
        return {Tri.TRUE: Tri.FALSE, Tri.FALSE: Tri.TRUE}.get(r, Tri.UNKNOWN)
    return _check_leaf(node["op"], _resolve(context, node["path"]), node)


def _validate(node: Any, *, depth: int, budget: list[int]) -> None:
    if depth > MAX_DEPTH:
        raise PredicateError(f"predicat prea adânc (max {MAX_DEPTH})")
    if not isinstance(node, dict):
        raise PredicateError("nodul de predicat trebuie să fie un obiect")
    budget[0] -= 1
    if budget[0] < 0:
        raise PredicateError(f"predicat prea mare (max {MAX_NODES} noduri)")

    group_keys = [k for k in ("all", "any", "not") if k in node]
    if group_keys:
        if len(node) != 1:
            raise PredicateError("un nod grup conține exact o cheie (all/any/not)")
        key = group_keys[0]
        if key == "not":
            _validate(node["not"], depth=depth + 1, budget=budget)
            return
        children = node[key]
        if not isinstance(children, list) or not children:
            raise PredicateError(f"'{key}' cere o listă nevidă")
        if len(children) > 20:
            raise PredicateError(f"'{key}' permite max 20 de copii")
        for child in children:
            _validate(child, depth=depth + 1, budget=budget)
        return

    op = node.get("op")
    if op not in _OPS:
        raise PredicateError(f"operator necunoscut: {op!r}")
    allowed = {"op", "path", "value", "values"}
    extra = set(node) - allowed
    if extra:
        raise PredicateError(f"câmpuri nepermise în predicat: {sorted(extra)}")

    path = node.get("path")
    if not isinstance(path, str) or not path or len(path) > 256:
        raise PredicateError("path lipsă sau prea lung")
    for seg in path.split("."):
        if not seg or len(seg) > MAX_PATH_SEGMENT:
            raise PredicateError(f"segment de path invalid: {seg!r}")

    if op == "exists":
        if "value" in node or "values" in node:
            raise PredicateError("'exists' nu ia value/values")
        return
    if op == "in":
        values = node.get("values")
        if not isinstance(values, list) or not values:
            raise PredicateError("'in' cere 'values' listă nevidă")
        if len(values) > MAX_LIST_ITEMS:
            raise PredicateError(f"'in' permite max {MAX_LIST_ITEMS} valori")
        return
    if "value" not in node:
        raise PredicateError(f"'{op}' cere câmpul 'value'")
    v = node["value"]
    if isinstance(v, str) and len(v) > MAX_STRING:
        raise PredicateError(f"valoare prea lungă (max {MAX_STRING} caractere)")
    if op in ("lt", "lte", "gt", "gte") and not _is_number(v):
        raise PredicateError(f"'{op}' cere o valoare numerică")
