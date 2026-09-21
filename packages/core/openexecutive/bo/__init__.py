"""BOAgents product slice — Valul 1.

Deterministic BoBots (no LLM required), the persisted Settings slice, and
the outbound BO telemetry adapter. Everything here is tenant-scoped through
``bo.identity`` and writes to its own SQLite database (``bo_agents.db``) so
the product's data stays separable from the host app's stores.

Scope notes (per BO-VAL1 mandate):
- BoBots run as *simulation only* — no external effects, no provider calls.
- The telemetry adapter defaults to a null transport; nothing is emitted
  unless an operator configures a transport explicitly.
"""
