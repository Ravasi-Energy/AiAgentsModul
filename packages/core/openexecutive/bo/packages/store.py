"""SQLite persistence for bo.package.v1 imports and downgrade approvals.

Lifecycle: QUARANTINED (import verified, content copied under
artifact-set digest) → DRAFT (admin promotion re-verifies the stored copy).
Rejected imports are persisted too (status REJECTED) so the refusal verdict
is auditable; they carry no stored content.

Idempotency: (tenant, package_id, version, artifact_set_digest) is UNIQUE —
the same bytes reimported return the same row with no new effect. The same
version with a different digest is a VERSION_CONFLICT at the service layer.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.bo.db import get_conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class NotFoundError(KeyError):
    pass


class StateError(Exception):
    """Illegal lifecycle transition → HTTP 409."""


def initialize_db(db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bo_package_imports (
                id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                package_id TEXT NOT NULL,
                version TEXT NOT NULL,
                kind TEXT NOT NULL,
                publisher_id TEXT NOT NULL,
                key_id TEXT NOT NULL,
                manifest_digest TEXT NOT NULL,
                artifact_set_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('QUARANTINED','DRAFT','REJECTED')),
                verdict_json TEXT NOT NULL,
                source_path TEXT NOT NULL,
                stored_path TEXT,
                approval_id TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            -- Unicitatea ține doar pentru rânduri vii: un REJECTED nu poate
            -- bloca reimportul aceleiași versiuni (altfel un refuz vechi ar
            -- produce IntegrityError/500 la primul import valid).
            CREATE UNIQUE INDEX IF NOT EXISTS ux_bo_pkg_imports_live
                ON bo_package_imports(tenant, package_id, version,
                                      artifact_set_digest)
                WHERE status != 'REJECTED';
            CREATE INDEX IF NOT EXISTS ix_bo_pkg_imports_tenant
                ON bo_package_imports(tenant, package_id);
            CREATE TABLE IF NOT EXISTS bo_package_approvals (
                id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                package_id TEXT NOT NULL,
                from_version TEXT NOT NULL,
                to_version TEXT NOT NULL,
                artifact_set_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                consumed_by_import TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def insert_import(row: dict[str, Any], db_path: Path | None = None) -> str:
    row = dict(row)
    row.setdefault("id", _new_id("imp"))
    row.setdefault("stored_path")
    row.setdefault("approval_id")
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO bo_package_imports
               (id, tenant, package_id, version, kind, publisher_id, key_id,
                manifest_digest, artifact_set_digest, status, verdict_json,
                source_path, stored_path, approval_id, actor,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["tenant"], row["package_id"], row["version"],
             row["kind"], row["publisher_id"], row["key_id"],
             row["manifest_digest"], row["artifact_set_digest"], row["status"],
             row["verdict_json"], row["source_path"], row["stored_path"],
             row["approval_id"], row["actor"], _now(), _now()),
        )
    return row["id"]


def _row_to_import(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["verdict"] = json.loads(d.pop("verdict_json"))
    return d


def get_import(tenant: str, import_id: str,
               db_path: Path | None = None) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM bo_package_imports WHERE id=? AND tenant=?",
            (import_id, tenant)).fetchone()
    if r is None:
        raise NotFoundError(import_id)
    return _row_to_import(r)


def find_import(tenant: str, package_id: str, version: str,
                db_path: Path | None = None) -> dict[str, Any] | None:
    """Existing NON-rejected import of this package version, if any."""
    with get_conn(db_path) as conn:
        r = conn.execute(
            """SELECT * FROM bo_package_imports
               WHERE tenant=? AND package_id=? AND version=?
                 AND status != 'REJECTED'
               ORDER BY created_at DESC LIMIT 1""",
            (tenant, package_id, version)).fetchone()
    return _row_to_import(r) if r else None


def list_imports(tenant: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM bo_package_imports
               WHERE tenant=? ORDER BY created_at DESC LIMIT 200""",
            (tenant,)).fetchall()
    return [_row_to_import(r) for r in rows]


def list_installed(tenant: str,
                   db_path: Path | None = None) -> dict[str, dict[str, str]]:
    """package_id → {version, artifactSetDigest} of quarantined/draft rows —
    the digest lets the verifier distinguish an idempotent reimport from a
    same-version conflict (contract §8)."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT package_id, version, artifact_set_digest
               FROM bo_package_imports
               WHERE tenant=? AND status IN ('QUARANTINED','DRAFT')""",
            (tenant,)).fetchall()
    return {r["package_id"]: {"version": r["version"],
                              "artifactSetDigest": r["artifact_set_digest"]}
            for r in rows}


def set_status(tenant: str, import_id: str, status: str,
               db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE bo_package_imports SET status=?, updated_at=?
               WHERE id=? AND tenant=?""",
            (status, _now(), import_id, tenant))


# ------------------------------------------------------------- approvals --

def insert_approval(row: dict[str, Any], db_path: Path | None = None) -> str:
    row = dict(row)
    row.setdefault("id", _new_id("apr"))
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO bo_package_approvals
               (id, tenant, package_id, from_version, to_version,
                artifact_set_digest, status, expires_at, created_by, created_at)
               VALUES (?,?,?,?,?,?,'active',?,?,?)""",
            (row["id"], row["tenant"], row["package_id"], row["from_version"],
             row["to_version"], row["artifact_set_digest"], row["expires_at"],
             row["created_by"], _now()),
        )
    return row["id"]


def _row_to_approval(r: Any) -> dict[str, Any]:
    return dict(r)


def list_approvals(tenant: str, package_id: str | None = None,
                   db_path: Path | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM bo_package_approvals WHERE tenant=?"
    params: list[Any] = [tenant]
    if package_id:
        q += " AND package_id=?"
        params.append(package_id)
    q += " ORDER BY created_at DESC LIMIT 200"
    with get_conn(db_path) as conn:
        return [_row_to_approval(r) for r in conn.execute(q, params).fetchall()]


def consume_approval(tenant: str, approval_id: str, import_id: str,
                     db_path: Path | None = None) -> bool:
    """Single-use: mark consumed only if still active and unconsumed."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """UPDATE bo_package_approvals
               SET consumed_at=?, consumed_by_import=?
               WHERE id=? AND tenant=? AND status='active' AND consumed_at IS NULL""",
            (_now(), import_id, approval_id, tenant))
        return cur.rowcount == 1


def revoke_approval(tenant: str, approval_id: str,
                    db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE bo_package_approvals SET status='revoked'
               WHERE id=? AND tenant=? AND consumed_at IS NULL""",
            (approval_id, tenant))
