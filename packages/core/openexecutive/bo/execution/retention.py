"""Retention sweep for execution data (VAL4-01).

Removes only FINISHED executions older than ``bo.exec.retention_days``
— active runs, in-flight leases and unfinished ledger entries are never
touched, and the host audit log is out of scope by design (same rule as
the simulation sweep).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from openexecutive.bo.db import get_conn
from openexecutive.bo.execution import store


def sweep(
    tenant: str,
    *,
    days: int | None = None,
    db_path: Path | None = None,
) -> int:
    """Delete finished runs (with their checkpoints/ledger/reservations)
    older than the threshold. Returns the number of runs removed."""
    if days is None:
        try:
            from openexecutive.bo.settings import store as settings_store

            days = int(
                settings_store.get_effective_value(
                    tenant, "bo.exec.retention_days", db_path=db_path
                )
            )
        except Exception:  # noqa: BLE001
            days = 90
    if days <= 0:
        return 0  # 0 = păstrează tot; sweep-ul nu șterge nimic
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    finished = (
        store.RUN_SUCCEEDED, store.RUN_FAILED, store.RUN_CANCELLED,
    )
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT run_id FROM bo_exec_runs WHERE tenant = ? "
            "AND state IN (?, ?, ?) AND finished_at IS NOT NULL "
            "AND finished_at < ?",
            (tenant, *finished, cutoff),
        ).fetchall()
        ids = [r["run_id"] for r in rows]
        for run_id in ids:
            for table in (
                "bo_checkpoints", "bo_effect_ledger",
                "bo_budget_reservations", "bo_exec_runs",
            ):
                conn.execute(
                    f"DELETE FROM {table} WHERE tenant = ? AND run_id = ?",
                    (tenant, run_id),
                )
    return len(ids)
