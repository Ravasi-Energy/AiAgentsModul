"""Every model call site must record a ``cache_event`` usage row.

``/audit/usage`` sums ``cache_event`` rows, so a model call that does not log
one is spend the cost view cannot see. The gap is easy to reintroduce — a new
workflow or integration calls the provider and simply forgets — and it is
invisible until someone reconciles the dashboard against a real invoice.

This test walks the source with ``ast`` and fails when a function issues a
provider call without reaching a usage-recording helper. It is deliberately
structural rather than a grep: ``executive.py`` logs through the
``_emit_cache_event`` wrapper, which a text search misses.

**What this guard does NOT prove.** It is a reachability-blind, name-based
check — the cheapest thing that reliably catches the "forgot to log" case:

- It does not prove the helper runs. A ``log_model_usage`` behind a condition
  that is never true still satisfies it.
- It does not check placement or arguments — logging the wrong ``message``,
  ``model`` or ``actor`` passes.
- It is function-scoped, not call-site-scoped. A function with two provider
  calls and one log satisfies it. That shape is legitimate today in
  ``agents/triage.py::triage``, whose two calls are mutually exclusive
  ``if``/``else`` branches converging on one log, but a loop that calls N
  times and logs once outside it would also pass.
- A call cancelled by ``asyncio.wait_for`` records nothing even though the
  tokens were billed. ``api/routes/chat.py``, ``integrations/response_gate.py``
  and ``workflows/inbound_resolver.py`` wrap their calls that way, so a
  timeout is billed-but-unrecorded by construction.

Those are the known limits; assertions about row contents live in
``test_usage_recording.py``.
"""
from __future__ import annotations

import ast
import pathlib

_TESTS_DIR = pathlib.Path(__file__).resolve().parents[2]
PACKAGE_ROOT = _TESTS_DIR / "openexecutive"
REPO_ROOT = _TESTS_DIR.parents[1]

# Provider-shim methods (``LLMProvider``) and the raw SDK chain used by the
# dual-path caller in agents/triage.py and by the standalone eval scripts.
_SHIM_METHODS = {"messages_create", "messages_stream"}
_RAW_CHAINS = {("messages", "create"), ("messages", "stream")}

# Helpers that actually WRITE a cache_event row. ``usage_counts`` is
# deliberately excluded: it only reads counters off a response, so treating it
# as proof of instrumentation would let a function that merely inspects usage
# satisfy the guard while emitting nothing.
_USAGE_HELPERS = {"log_model_usage", "_emit_cache_event"}

# Directories scanned for provider calls. Roots are (path, label) pairs.
_SCAN_ROOTS = [(PACKAGE_ROOT, "openexecutive"), (REPO_ROOT / "evals", "evals")]

# Paths exempt from the rule, each with a reason. Keep this list short — an
# entry here is spend that /audit/usage cannot report. Prefixes must end in
# "/" so a sibling like ``providers_shim.py`` is not silently exempted.
_ALLOWLIST: dict[str, str] = {
    "openexecutive/providers/": (
        "provider shim — the call plumbing itself, with no actor to attribute "
        "a row to; every caller records the row instead"
    ),
    "evals/": (
        "standalone eval harness, run by hand against a deployed instance "
        "rather than in-process — it has no audit DB to write to. Its spend is "
        "real but is operator tooling, not the running system's own cost"
    ),
}


def _is_model_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    attr = node.func
    if attr.attr in _SHIM_METHODS:
        return True
    return (
        isinstance(attr.value, ast.Attribute)
        and (attr.value.attr, attr.attr) in _RAW_CHAINS
    )


def _own_scope(fn: ast.AST) -> list[ast.AST]:
    """Nodes belonging to ``fn`` itself, not to functions nested inside it.

    ``ast.walk`` descends into nested ``def``s, which would let a never-called
    inner closure containing ``log_model_usage`` vouch for its parent.
    """
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _records_usage(fn: ast.AST) -> bool:
    for node in _own_scope(fn):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in _USAGE_HELPERS:
            return True
    return False


def _allowlisted(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in _ALLOWLIST)


def _iter_call_sites() -> list[tuple[str, str, int, str]]:
    """(rel_path, func_name, lineno, 'ok'|'missing') for every provider call."""
    sites: list[tuple[str, str, int, str]] = []
    for root, label in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = f"{label}/{path.relative_to(root).as_posix()}"
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                calls = [n for n in _own_scope(fn) if _is_model_call(n)]
                if not calls:
                    continue
                status = "ok" if _records_usage(fn) else "missing"
                for call in calls:
                    sites.append((rel, fn.name, call.lineno, status))
    return sites


def test_every_model_call_site_records_usage() -> None:
    offenders = [
        f"{rel}:{lineno} in {fn}()"
        for rel, fn, lineno, status in _iter_call_sites()
        if status == "missing" and not _allowlisted(rel)
    ]
    assert not offenders, (
        "These model calls do not record a cache_event usage row, so their "
        "tokens and cost are invisible to GET /audit/usage. Add "
        "log_model_usage(message, model=..., actor=...) after the call with a "
        "distinct actor, or add a justified entry to _ALLOWLIST:\n  "
        + "\n  ".join(offenders)
    )


def test_scanner_actually_finds_call_sites() -> None:
    """Guard the guard: a broken matcher would silently pass the test above.

    Pinned just below the current count so a matcher that stops recognising a
    call shape fails here rather than reporting a clean sweep.
    """
    in_package = [s for s in _iter_call_sites() if s[0].startswith("openexecutive/")]
    assert len(in_package) >= 28, (
        f"AST matcher found only {len(in_package)} model call sites in the "
        "package; it previously found 28. A call shape is no longer recognised."
    )


def test_allowlist_prefixes_are_directory_scoped() -> None:
    """A prefix without a trailing slash would exempt sibling modules too."""
    bad = [p for p in _ALLOWLIST if not p.endswith("/")]
    assert not bad, f"_ALLOWLIST prefixes must end in '/': {bad}"
