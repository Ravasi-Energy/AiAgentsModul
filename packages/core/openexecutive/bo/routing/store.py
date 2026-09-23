"""SQLite persistence for the observe-mode router (VAL3-01).

Two tenants of truth, deliberately separate:

* ``bo_model_catalog`` — the administered catalog (authority: BOAgents).
  Every write bumps the tenant's ``catalog_version`` counter and carries a
  per-row CAS ``version``, so concurrent editors lose deterministically.
* ``bo_route_observations`` — one durable row per observed model call,
  written BEFORE the telemetry emit so a lost receiver never loses the
  observation; ``delivered``/``delivery_error`` record the outcome and
  ``flush_undelivered`` retries on demand.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.bo.db import get_conn
from openexecutive.bo.routing.catalog import (
    CatalogEntry,
    CatalogValidationError,
    Cost,
    Quality,
    validate_fields,
)


class NotFoundError(KeyError):
    pass


class ConflictError(Exception):
    """expected_version did not match the stored version → HTTP 409."""


class DuplicateEntryError(Exception):
    """Same (provider, model_id, model_version) already cataloged → HTTP 409."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def initialize_db(db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_model_catalog (
                entry_id        TEXT NOT NULL,
                tenant          TEXT NOT NULL,
                provider        TEXT NOT NULL,
                model_id        TEXT NOT NULL,
                model_version   TEXT,
                state           TEXT NOT NULL,
                capabilities    TEXT NOT NULL,
                regions         TEXT NOT NULL,
                cost_json       TEXT NOT NULL,
                quality_json    TEXT,
                purpose         TEXT NOT NULL,
                source          TEXT NOT NULL,
                version         INTEGER NOT NULL,
                updated_by      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (tenant, entry_id),
                UNIQUE (tenant, provider, model_id, model_version)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_catalog_meta (
                tenant      TEXT PRIMARY KEY,
                version     INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_route_observations (
                obs_id           TEXT NOT NULL,
                tenant           TEXT NOT NULL,
                occurred_at      TEXT NOT NULL,
                correlation_id   TEXT NOT NULL,
                task_kind        TEXT NOT NULL,
                actor_ref        TEXT NOT NULL,
                policy_version   TEXT NOT NULL,
                catalog_version  TEXT NOT NULL,
                decision         TEXT NOT NULL,
                met_bar          INTEGER NOT NULL,
                reasons_json     TEXT NOT NULL,
                recommendation   TEXT,
                actual_route     TEXT,
                cost_estimate    TEXT,
                measured         TEXT,
                billed           TEXT,
                detail_json      TEXT NOT NULL,
                event_json       TEXT NOT NULL,
                delivered        INTEGER NOT NULL DEFAULT 0,
                delivery_error   TEXT,
                PRIMARY KEY (tenant, obs_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS bo_route_obs_tenant_time "
            "ON bo_route_observations (tenant, occurred_at)"
        )


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

def _entry_from_row(row: Any) -> CatalogEntry:
    quality_raw = row["quality_json"]
    return CatalogEntry(
        entry_id=row["entry_id"],
        provider=row["provider"],
        model_id=row["model_id"],
        model_version=row["model_version"],
        state=row["state"],
        capabilities=tuple(json.loads(row["capabilities"])),
        regions=tuple(json.loads(row["regions"])),
        cost=Cost.from_dict(json.loads(row["cost_json"])),
        quality=Quality.from_dict(json.loads(quality_raw))
        if quality_raw is not None
        else None,
        purpose=row["purpose"],
        source=row["source"],
        version=int(row["version"]),
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
    )


def catalog_version(tenant: str, db_path: Path | None = None) -> int:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT version FROM bo_catalog_meta WHERE tenant = ?", (tenant,)
        ).fetchone()
    return 0 if row is None else int(row["version"])


def _bump_catalog(conn: Any, tenant: str) -> int:
    row = conn.execute(
        "SELECT version FROM bo_catalog_meta WHERE tenant = ?", (tenant,)
    ).fetchone()
    new_version = 1 if row is None else int(row["version"]) + 1
    conn.execute(
        "INSERT INTO bo_catalog_meta (tenant, version) VALUES (?, ?) "
        "ON CONFLICT (tenant) DO UPDATE SET version = excluded.version",
        (tenant, new_version),
    )
    return new_version


def list_catalog(tenant: str, db_path: Path | None = None) -> list[CatalogEntry]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bo_model_catalog WHERE tenant = ? "
            "ORDER BY provider, model_id, model_version",
            (tenant,),
        ).fetchall()
    return [_entry_from_row(r) for r in rows]


def get_entry(
    tenant: str, entry_id: str, db_path: Path | None = None
) -> CatalogEntry:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_model_catalog WHERE tenant = ? AND entry_id = ?",
            (tenant, entry_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(entry_id)
    return _entry_from_row(row)


def _audit_catalog(
    tenant: str, action: str, entry: CatalogEntry, *, actor: str
) -> None:
    from openexecutive.audit import log_event

    log_event(
        "bo_catalog_change",
        f"catalog {action}: {entry.provider}/{entry.model_id} v{entry.version}",
        actor=actor,
        details={
            "tenant": tenant,
            "action": action,
            "entry_id": entry.entry_id,
            "provider": entry.provider,
            "model_id": entry.model_id,
            "model_version": entry.model_version,
            "state": entry.state,
            "version": entry.version,
        },
    )


def create_entry(
    tenant: str,
    fields: dict[str, Any],
    *,
    actor: str,
    db_path: Path | None = None,
) -> CatalogEntry:
    v = validate_fields(**fields)
    entry = CatalogEntry(
        entry_id=f"cat_{uuid.uuid4().hex[:16]}",
        version=1,
        updated_by=actor,
        updated_at=_now(),
        **v,
    )
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        clash = conn.execute(
            "SELECT entry_id FROM bo_model_catalog WHERE tenant = ? "
            "AND provider = ? AND model_id = ? AND model_version IS ?",
            (tenant, entry.provider, entry.model_id, entry.model_version),
        ).fetchone()
        if clash is not None:
            raise DuplicateEntryError(
                f"{entry.provider}/{entry.model_id} "
                f"(versiune {entry.model_version or 'necunoscută'}) există deja"
            )
        _bump_catalog(conn, tenant)
        conn.execute(
            """
            INSERT INTO bo_model_catalog (
                entry_id, tenant, provider, model_id, model_version, state,
                capabilities, regions, cost_json, quality_json, purpose,
                source, version, updated_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_id, tenant, entry.provider, entry.model_id,
                entry.model_version, entry.state,
                json.dumps(list(entry.capabilities)),
                json.dumps(list(entry.regions)),
                json.dumps(entry.cost.to_dict()),
                json.dumps(entry.quality.to_dict())
                if entry.quality is not None
                else None,
                entry.purpose, entry.source,
                entry.version, actor, entry.updated_at,
            ),
        )
    _audit_catalog(tenant, "create", entry, actor=actor)
    return entry


def update_entry(
    tenant: str,
    entry_id: str,
    fields: dict[str, Any],
    *,
    expected_version: int,
    actor: str,
    db_path: Path | None = None,
) -> CatalogEntry:
    v = validate_fields(**fields)
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT version FROM bo_model_catalog WHERE tenant = ? AND entry_id = ?",
            (tenant, entry_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(entry_id)
        current_version = int(row["version"])
        if expected_version != current_version:
            raise ConflictError(
                f"expected_version={expected_version} dar versiunea curentă "
                f"este {current_version}"
            )
        clash = conn.execute(
            "SELECT entry_id FROM bo_model_catalog WHERE tenant = ? "
            "AND provider = ? AND model_id = ? AND model_version IS ? "
            "AND entry_id <> ?",
            (tenant, v["provider"], v["model_id"], v["model_version"], entry_id),
        ).fetchone()
        if clash is not None:
            raise DuplicateEntryError(
                f"{v['provider']}/{v['model_id']} există deja pe altă intrare"
            )
        _bump_catalog(conn, tenant)
        conn.execute(
            """
            UPDATE bo_model_catalog SET
                provider = ?, model_id = ?, model_version = ?, state = ?,
                capabilities = ?, regions = ?, cost_json = ?, quality_json = ?,
                purpose = ?, source = ?, version = ?, updated_by = ?,
                updated_at = ?
            WHERE tenant = ? AND entry_id = ? AND version = ?
            """,
            (
                v["provider"], v["model_id"], v["model_version"], v["state"],
                json.dumps(list(v["capabilities"])),
                json.dumps(list(v["regions"])),
                json.dumps(v["cost"].to_dict()),
                json.dumps(v["quality"].to_dict())
                if v["quality"] is not None
                else None,
                v["purpose"], v["source"], current_version + 1,
                actor, _now(), tenant, entry_id, current_version,
            ),
        )
    entry = get_entry(tenant, entry_id, db_path=db_path)
    _audit_catalog(tenant, "update", entry, actor=actor)
    return entry


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #

def record_observation(
    tenant: str,
    obs: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> str:
    """Persist one observation row durably (delivered=0 until emit succeeds)."""
    obs_id = f"obs_{uuid.uuid4().hex[:20]}"
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bo_route_observations (
                obs_id, tenant, occurred_at, correlation_id, task_kind,
                actor_ref, policy_version, catalog_version, decision,
                met_bar, reasons_json, recommendation, actual_route,
                cost_estimate, measured, billed, detail_json, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs_id, tenant, obs["occurred_at"], obs["correlation_id"],
                obs["task_kind"], obs["actor_ref"], obs["policy_version"],
                obs["catalog_version"], obs["decision"], int(obs["met_bar"]),
                json.dumps(obs["reasons"]),
                json.dumps(obs["recommendation"])
                if obs.get("recommendation") is not None
                else None,
                json.dumps(obs["actual_route"])
                if obs.get("actual_route") is not None
                else None,
                json.dumps(obs["cost_estimate"])
                if obs.get("cost_estimate") is not None
                else None,
                json.dumps(obs["measured"])
                if obs.get("measured") is not None
                else None,
                json.dumps(obs["billed"])
                if obs.get("billed") is not None
                else None,
                json.dumps(obs["detail"]),
                json.dumps(obs["event"]),
            ),
        )
    return obs_id


def mark_delivered(
    tenant: str,
    obs_id: str,
    *,
    error: str | None,
    db_path: Path | None = None,
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE bo_route_observations SET delivered = ?, delivery_error = ? "
            "WHERE tenant = ? AND obs_id = ?",
            (0 if error else 1, error, tenant, obs_id),
        )


def _obs_from_row(row: Any) -> dict[str, Any]:
    return {
        "obs_id": row["obs_id"],
        "occurred_at": row["occurred_at"],
        "correlation_id": row["correlation_id"],
        "task_kind": row["task_kind"],
        "actor_ref": row["actor_ref"],
        "policy_version": row["policy_version"],
        "catalog_version": row["catalog_version"],
        "decision": row["decision"],
        "met_bar": bool(row["met_bar"]),
        "reasons": json.loads(row["reasons_json"]),
        "recommendation": json.loads(row["recommendation"])
        if row["recommendation"] is not None
        else None,
        "actual_route": json.loads(row["actual_route"])
        if row["actual_route"] is not None
        else None,
        "cost_estimate": json.loads(row["cost_estimate"])
        if row["cost_estimate"] is not None
        else None,
        "measured": json.loads(row["measured"])
        if row["measured"] is not None
        else None,
        "billed": json.loads(row["billed"]) if row["billed"] is not None else None,
        "detail": json.loads(row["detail_json"]),
        "event": json.loads(row["event_json"]),
        "delivered": bool(row["delivered"]),
        "delivery_error": row["delivery_error"],
    }


def list_observations(
    tenant: str,
    *,
    decision: str | None = None,
    task_kind: str | None = None,
    met_bar: bool | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM bo_route_observations WHERE tenant = ?"
    params: list[Any] = [tenant]
    if decision is not None:
        sql += " AND decision = ?"
        params.append(decision)
    if task_kind is not None:
        sql += " AND task_kind = ?"
        params.append(task_kind)
    if met_bar is not None:
        sql += " AND met_bar = ?"
        params.append(int(met_bar))
    sql += " ORDER BY occurred_at DESC, obs_id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_obs_from_row(r) for r in rows]


def get_observation(
    tenant: str, obs_id: str, db_path: Path | None = None
) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_route_observations WHERE tenant = ? AND obs_id = ?",
            (tenant, obs_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(obs_id)
    return _obs_from_row(row)


def undelivered(
    tenant: str, db_path: Path | None = None
) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bo_route_observations WHERE tenant = ? "
            "AND delivered = 0 ORDER BY occurred_at",
            (tenant,),
        ).fetchall()
    return [_obs_from_row(r) for r in rows]


def sweep_observations(
    tenant: str, retention_days: int, db_path: Path | None = None
) -> int:
    """Delete observations older than the retention window. Never touches
    audit or other tables."""
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM bo_route_observations WHERE tenant = ? "
            "AND occurred_at < ?",
            (tenant, cutoff),
        )
        return cur.rowcount


def observation_stats(
    tenant: str, db_path: Path | None = None
) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN delivered = 0 THEN 1 ELSE 0 END) AS pending, "
            "SUM(CASE WHEN met_bar = 1 THEN 1 ELSE 0 END) AS met, "
            "MAX(occurred_at) AS last_at "
            "FROM bo_route_observations WHERE tenant = ?",
            (tenant,),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "pending_delivery": int(row["pending"] or 0),
        "met_bar": int(row["met"] or 0),
        "last_at": row["last_at"],
    }


__all__ = [
    "CatalogValidationError",
    "ConflictError",
    "DuplicateEntryError",
    "NotFoundError",
    "catalog_version",
    "create_entry",
    "get_entry",
    "get_observation",
    "initialize_db",
    "list_catalog",
    "list_observations",
    "mark_delivered",
    "observation_stats",
    "record_observation",
    "sweep_observations",
    "undelivered",
    "update_entry",
]
