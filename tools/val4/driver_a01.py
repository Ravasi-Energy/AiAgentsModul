"""VAL4-01 REM-01 — driverul real BOAgents (A01) pentru gate-ul comun.

Rulează ÎN worktree-ul A01 fixat la SHA, cu venv-ul A01
(packages/core/.venv). CLI compatibil cu contractul gate-exec.sh:

    python tools/val4/driver_a01.py flow --wt <worktree> --db <sqlite> \
        --tenant tenant-alpha [--steps N] [--execute] [--correlation C] \
        [--guardian-ref MND]
    python tools/val4/driver_a01.py work --wt <worktree> --db <sqlite>
    python tools/val4/driver_a01.py deliver --wt <worktree> --db <sqlite>
    python tools/val4/driver_a01.py stare --wt <worktree> --db <sqlite>

Calea reală, fără niciun eveniment construit manual:
settings → (opțional) mandat Guardian → create_mandate(guardian_ref) →
submit_execution → work_once cu SyntheticCounterProvider (contor
persistent) → plicurile bo.execution-control.event.v1 ies prin
bo_telemetry_outbox → deliver_pending le postează byte-identic la
receptorul /v1/execution-events. La fiecare frontieră de efect motorul
reverifică GET /v1/mandates/{ref}/status în Guardian.

Mediu (setat înainte de import): BOAGENTS_DB_PATH, BO_TENANT_ID,
BO_TELEMETRY_ENABLED=1, BO_TELEMETRY_ENDPOINT (URL complet
/v1/execution-events), BO_TELEMETRY_TOKEN (execobs:write),
BO_TELEMETRY_PRODUCER_ID, BO_INSTALLATION_ID.

Legătura Guardian:
  GATE_BASE sau baza derivată din BO_TELEMETRY_ENDPOINT → endpointul de
  autorizare; BO_GUARDIAN_ADMIN_TOKEN (implicit „exadmin", credențialul
  sintetic execpolicy al gate-ului) → emiterea/descoperirea mandatului.
  Rezolvarea guardian_ref: --guardian-ref → BO_GUARDIAN_MANDATE_REF →
  „mnd-gate-val402" dacă e ACTIVE la Guardian → cel mai nou mandat
  BOAgents ACTIVE → emis proaspăt „mnd-a01-*". Fără legătură configurată,
  execuția rămâne locală (documentat).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from decimal import Decimal


def _setup(wt: str, db: str) -> None:
    os.environ["BOAGENTS_DB_PATH"] = db
    sys.path.insert(0, os.path.join(wt, "packages", "core"))
    from openexecutive.bo.db import initialize_db
    initialize_db()


def _set(tenant: str, key: str, value, actor: str = "driver-gate") -> None:
    """Setare reală prin magazinul de setări (CAS cu versiunea curentă)."""
    from openexecutive.bo.db import get_conn
    from openexecutive.bo.settings import store as settings_store
    with get_conn() as conn:
        row = conn.execute(
            "SELECT version FROM bo_settings WHERE tenant = ? AND key = ?",
            (tenant, key),
        ).fetchone()
    settings_store.set_value(
        tenant, key, value,
        expected_version=0 if row is None else int(row["version"]),
        actor=actor,
    )


def _guardian_base() -> str:
    """URL-ul de bază Guardian: GATE_BASE, apoi derivat din endpointul
    complet de livrare, apoi BO_GUARDIAN_URL."""
    if os.environ.get("GATE_BASE"):
        return os.environ["GATE_BASE"].rstrip("/")
    ep = os.environ.get("BO_TELEMETRY_ENDPOINT", "").rstrip("/")
    if ep:
        return ep.split("/v1/")[0]
    return os.environ.get("BO_GUARDIAN_URL", "").rstrip("/")


def _admin_token() -> str:
    # Credențialul sintetic execpolicy al gate-ului — public în
    # gate-exec.sh; valid doar contra unui Guardian de dev cu fixture.
    return os.environ.get("BO_GUARDIAN_ADMIN_TOKEN", "exadmin")


def _http(method: str, url: str, token: str, body=None,
          timeout: float = 10.0):
    req = urllib.request.Request(
        url, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:300]
        except Exception:
            detail = ""
        return exc.code, detail
    except Exception as exc:  # noqa: BLE001 — Guardian jos/net
        return 0, str(exc)[:200]


def _mandate_status(base: str, tenant: str, ref: str, token: str):
    s, d = _http("GET", f"{base}/v1/mandates/{ref}/status?tenant={tenant}",
                 token)
    return d if s == 200 and isinstance(d, dict) else None


def _resolve_guardian_ref(base: str, tenant: str, args) -> str | None:
    """Ordinea: arg → env → mnd-gate-val402 (mandatul sondei gate) →
    cel mai nou mandat BOAgents ACTIVE → emis proaspăt."""
    if getattr(args, "guardian_ref", None):
        return args.guardian_ref
    if os.environ.get("BO_GUARDIAN_MANDATE_REF"):
        return os.environ["BO_GUARDIAN_MANDATE_REF"]
    if not base:
        return None
    admin = _admin_token()
    st = _mandate_status(base, tenant, "mnd-gate-val402", admin)
    if st and st.get("status") == "ACTIVE":
        return "mnd-gate-val402"
    s, d = _http("GET", f"{base}/v1/mandates?tenant={tenant}", admin)
    if s == 200 and isinstance(d, dict):
        candidates = [
            m for m in d.get("mandates", [])
            if m.get("status") == "ACTIVE"
            and m.get("product", "BOAgents") == "BOAgents"
        ]
        if candidates:
            return sorted(
                candidates, key=lambda m: str(m.get("updatedAt", ""))
            )[-1]["mandateId"]
    # Nimic activ — emitem un mandat proaspăt (dovada „mandate creation").
    mid = f"mnd-a01-{uuid.uuid4().hex[:10]}"
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    doc = {
        "schemaVersion": "bo.execution-control.mandate.v1",
        "mandateId": mid, "mandateVersion": 1, "status": "ACTIVE",
        "tenantRef": tenant, "principalRef": "driver-gate",
        "product": "BOAgents",
        "installationId": os.environ.get(
            "BO_INSTALLATION_ID", "inst-a01-alpha"),
        "parentRef": None,
        "issuedAt": now.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"),
        "expiresAt": (now + timedelta(hours=1)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "policyVersion": "pol_0", "correlationId": f"corr-{mid}",
        "allowed": {"resources": ["synth.*", "tool:counter.increment",
                                  "tool:catalog.read"],
                    "actions": ["read", "checkpoint", "effect.intent",
                                "increment"]},
        "limits": {"maxSteps": 20, "maxDepth": 1, "maxConcurrency": 2,
                   "budget": {"amount": "50.00", "currency": "USD"}},
        "checkpoint": {"required": True, "everySteps": 5},
    }
    s, d = _http("POST", f"{base}/v1/mandates?tenant={tenant}", admin,
                 {"mandate": doc, "reason": "emis de driverul A01"})
    return mid if s in (200, 201) else None


def _configure_guardian(tenant: str, base: str, bound: bool) -> None:
    """Setările bo.exec.guardian.* prin magazinul real — persistate,
    auditate, vizibile în UI."""
    if not base:
        return
    _set(tenant, "bo.exec.guardian_endpoint", base)
    _set(tenant, "bo.exec.guardian_secret_ref", "BO_TELEMETRY_TOKEN")
    _set(tenant, "bo.exec.guardian_auth_required", bool(bound))


def _mandate(tenant: str, actor: str, guardian_ref: str | None):
    from datetime import UTC, datetime, timedelta
    from openexecutive.bo.execution import store
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    return store.create_mandate(
        tenant,
        {
            "allowed_resources": ["synth.*"],
            "allowed_actions": ["increment"],
            "budget_limit": "10",
            "concurrency_limit": 2,
            "max_steps": 10,
            "max_depth": 1,
            "expires_at": expiry,
        },
        parent=None, principal_ref=actor,
        policy_version=0, actor=actor, max_depth_cap=3,
        guardian_ref=guardian_ref,
    )


def _outbox_stats(tenant: str) -> dict:
    from openexecutive.bo.db import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT delivered, COUNT(*) n FROM bo_telemetry_outbox "
            "WHERE tenant = ? GROUP BY delivered", (tenant,),
        ).fetchall()
    return {str(r["delivered"]): r["n"] for r in rows}


def cmd_flow(args) -> dict:
    """Mandat (+legătură Guardian) + execuție reală: pași sintetici,
    contor persistent, outbox."""
    _setup(args.wt, args.db)
    from openexecutive.bo.execution import engine, store
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    from openexecutive.bo.routing import delivery

    tenant = args.tenant
    _set(tenant, "bo.exec.enabled", True)
    base = _guardian_base()
    gref = _resolve_guardian_ref(base, tenant, args)
    _configure_guardian(tenant, base, bound=bool(gref))
    provider = SyntheticCounterProvider(idempotent=True)
    mandate = _mandate(tenant, actor="driver-gate", guardian_ref=gref)
    steps = [
        {"action": "increment", "resource": "synth.counter",
         "payload": {"amount": 1}}
        for _ in range(args.steps)
    ]
    run = engine.submit_execution(
        tenant, mandate.mandate_id, steps,
        budget_amount=Decimal("5"), correlation_id=args.correlation,
        actor="driver-gate",
    )
    outcome = None
    if args.execute:
        outcome = engine.work_once(
            tenant, provider=provider, worker_id=args.worker)
    delivered = delivery.deliver_pending(tenant)
    return {
        "tenant": tenant, "mandate_id": mandate.mandate_id,
        "guardian_ref": gref, "guardian_base": base or None,
        "run_id": run["run_id"], "work": outcome,
        "final_state": store.get_run(tenant, run["run_id"])["state"],
        "efecte_furnizor": provider.total(tenant),
        "deliver": delivered, "outbox": _outbox_stats(tenant),
    }


def cmd_work(args) -> dict:
    """Un ciclu de lucru pe run-urile existente — faza după revocare."""
    _setup(args.wt, args.db)
    from openexecutive.bo.execution import engine
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    tenant = args.tenant
    provider = SyntheticCounterProvider(idempotent=True)
    outcome = engine.work_once(
        tenant, provider=provider, worker_id=args.worker)
    return {"tenant": tenant, "work": outcome,
            "efecte_furnizor": provider.total(tenant),
            "outbox": _outbox_stats(tenant)}


def cmd_deliver(args) -> dict:
    """Doar ciclul de livrare — retry după repornirea receptorului."""
    _setup(args.wt, args.db)
    from openexecutive.bo.routing import delivery
    tenant = args.tenant
    return {"tenant": tenant, "deliver": delivery.deliver_pending(tenant),
            "outbox": _outbox_stats(tenant)}


def cmd_stare(args) -> dict:
    _setup(args.wt, args.db)
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    tenant = args.tenant
    return {"tenant": tenant,
            "efecte_furnizor": SyntheticCounterProvider(
                idempotent=True).total(tenant),
            "outbox": _outbox_stats(tenant)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["flow", "work", "deliver", "stare"],
                    help="work = ciclu de lucru pe run-urile existente")
    ap.add_argument("--wt", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--tenant", default="tenant-alpha")
    ap.add_argument("--worker", default="gate-w1")
    ap.add_argument("--steps", type=int, default=2)
    ap.add_argument("--correlation", default=None)
    ap.add_argument("--guardian-ref", default=None)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    out = {
        "flow": cmd_flow, "work": cmd_work,
        "deliver": cmd_deliver, "stare": cmd_stare,
    }[args.cmd](args)
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
