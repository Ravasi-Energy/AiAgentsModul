"""Signed diagnostic activation over the existing deterministic runtime.

Only the built-in diagnostic grammar is executable. Package artifacts are data,
never imported Python, shell commands or arbitrary network destinations.
"""
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from openexecutive.bo.db import get_conn
from openexecutive.bo.execution import engine, store
from openexecutive.bo.identity import Identity, require
from openexecutive.bo.packages import service as packages
from openexecutive.bo.packages import store as package_store
from openexecutive.bo.packages.verify import verify_package
from openexecutive.bo.settings import store as settings

PACKAGE_ID = "pkg.synthetic-erp"
RESOURCE = "synth.erp"
ACTION = "diagnose"
CONTENT = {
    "schema_version": "bo.bobot.v1", "trigger": {"type": "manual"},
    "steps": [{"id": "diagnose", "type": "note", "message": "Diagnostic ERP sintetic: health, versiune, coadă și receipt; fără LLM."}],
    "capability_refs": ["synth_erp:diagnose"], "policy_refs": [],
}


def initialize_db(db_path=None):
    with get_conn(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_pilot_activation (
            tenant TEXT PRIMARY KEY, import_id TEXT NOT NULL, version INTEGER NOT NULL,
            active INTEGER NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_pilot_observations (
            tenant TEXT NOT NULL, run_id TEXT NOT NULL, event_json TEXT NOT NULL,
            PRIMARY KEY(tenant,run_id))""")


def configuration(tenant, db_path=None):
    from openexecutive.bo.settings.registry import REGISTRY
    return {k.removeprefix("bo.pilot."): settings.get_effective_value(tenant, k, db_path=db_path)
            for k in REGISTRY if k.startswith("bo.pilot.")}


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def activation(tenant, db_path=None):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM bo_pilot_activation WHERE tenant=?", (tenant,)).fetchone()
    return dict(row) if row else None


def verify_import(tenant, import_id, db_path=None):
    row = package_store.get_import(tenant, import_id, db_path=db_path)
    if row["status"] != "DRAFT" or row["package_id"] != PACKAGE_ID:
        raise package_store.StateError("Pilotul cere pachetul sintetic verificat și promovat la DRAFT")
    identity = Identity("pilot-verifier", tenant, "admin", True, "shared_secret")
    verdict = verify_package(Path(row["stored_path"]), packages._trust_registry(identity, db_path),
                             tenant_ref=tenant, host_version=packages.BO_PRODUCT_VERSION)
    if not verdict.accepted or verdict.manifest_digest != row["manifest_digest"]:
        raise package_store.StateError(f"Verificare curentă refuzată: {verdict.code}")
    manifest = json.loads((Path(row["stored_path"]) / "manifest.json").read_text())
    if set(manifest["artifactDigests"]) != {"bots/diagnostic.json"} or manifest["requestedCapabilities"] != ["synth_erp:diagnose"]:
        raise package_store.StateError("Pachetul nu declară exact diagnosticul sintetic")
    if json.loads((Path(row["stored_path"]) / "bots/diagnostic.json").read_text()) != CONTENT:
        raise package_store.StateError("Gramatică diagnostic incompatibilă; scripturile nu se execută")
    return row


def change_activation(identity, import_id, active, expected_version, reason, db_path=None):
    require(identity, "packages:write")
    if active:
        verify_import(identity.tenant, import_id, db_path)
    now = datetime.now(UTC).isoformat()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM bo_pilot_activation WHERE tenant=?", (identity.tenant,)).fetchone()
        if (old["version"] if old else 0) != expected_version:
            raise store.ConflictError("Activarea a fost modificată; reîncarcă înainte de salvare")
        if not active and (not old or old["import_id"] != import_id):
            raise package_store.StateError("Activare inexistentă")
        conn.execute("""INSERT INTO bo_pilot_activation VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(tenant) DO UPDATE SET import_id=excluded.import_id,
            version=excluded.version,active=excluded.active,actor=excluded.actor,
            reason=excluded.reason,updated_at=excluded.updated_at""",
                     (identity.tenant, import_id, expected_version + 1, int(active), identity.actor, reason, now))
    packages._audit(identity.tenant, identity.actor, "bo_pilot_activation",
                    "Activare diagnostic sintetic" if active else "Dezactivare diagnostic; istoric păstrat",
                    {"import_id": import_id, "version": expected_version + 1, "active": active, "reason": reason})
    return activation(identity.tenant, db_path)


def guard(tenant, payload=None, db_path=None):
    from openexecutive.bo.pilot.config import endpoint
    config = configuration(tenant, db_path)
    if not config["enabled"] or config["profile"] != "synthetic-loopback":
        raise package_store.StateError("Pilot dezactivat sau profil sintetic neconfigurat")
    target = endpoint(config["endpoint"])
    if not target or target not in json.loads(config["allowlist"]):
        raise package_store.StateError("Endpointul nu este în allowlist-ul administrat")
    active = activation(tenant, db_path)
    if not active or not active["active"]:
        raise package_store.StateError("Pachetul nu este activat")
    row = verify_import(tenant, active["import_id"], db_path)
    if payload is not None and (payload.get("activation_version") != active["version"]
                               or payload.get("config_hash") != config_hash(config)
                               or payload.get("manifest_digest") != row["manifest_digest"]):
        raise package_store.StateError("Configurația/activarea diferă de plan; fără retrimitere pe alt endpoint")
    return config, active, row


def submit(identity, mandate_id, correlation_id=None, db_path=None):
    require(identity, "execution:write")
    config, active, row = guard(identity.tenant, db_path=db_path)
    if config["supervision"] == "required" and not any(
        m.guardian_ref for m in store.mandate_chain(identity.tenant, mandate_id, db_path=db_path)
    ):
        raise package_store.StateError("Pilotul cere autoritate Guardian; leagă un mandat existent")
    return engine.submit_execution(identity.tenant, mandate_id,
        [{"action": ACTION, "resource": RESOURCE, "payload": {
            "amount": 1, "activation_version": active["version"],
            "config_hash": config_hash(config), "manifest_digest": row["manifest_digest"],
        }}], budget_amount=Decimal("1"), correlation_id=correlation_id,
        actor=identity.actor, db_path=db_path)


def status(identity, db_path=None):
    require(identity, "execution:read")
    runs = [r for r in store.list_runs(identity.tenant, db_path=db_path)
            if any(s.get("resource") == RESOURCE for s in r["steps"])]
    config = configuration(identity.tenant, db_path)
    with get_conn(db_path) as conn:
        observations = {r["run_id"]: json.loads(r["event_json"]) for r in conn.execute(
            "SELECT * FROM bo_pilot_observations WHERE tenant=?", (identity.tenant,))}
    for run in runs:
        event = observations.get(run["run_id"])
        run["observation"] = event
        run["ledger"] = store.list_ledger(identity.tenant, run["run_id"], db_path=db_path)
        run["health"] = "UNKNOWN"
        if event:
            age = (datetime.now(UTC) - datetime.fromisoformat(event["observedAt"].replace("Z", "+00:00"))).total_seconds()
            run["health"] = "STALE" if age > config["stale_s"] else (
                "DEGRADED" if event["service"]["queuePending"] > config["max_queue"] else "HEALTHY")
        if run["state"] != store.RUN_SUCCEEDED:
            run["health"] = "UNKNOWN"
    return {"synthetic": True, "role": identity.role, "activation": activation(identity.tenant, db_path),
            "config": config, "runs": runs,
            "note": "Observabilitatea nu acordă autoritate. UNKNOWN cere readback; fără ERP real sau LLM."}
