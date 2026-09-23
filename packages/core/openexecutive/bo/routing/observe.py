"""Observe-mode hook — binds the deterministic engine to the real model-call
flow WITHOUT changing the route (VAL3-01, common decision §1).

Called from ``audit.usage.log_model_usage`` after every completed provider
call. It computes what the administered catalog + tenant policy would have
recommended, records the observation durably, then tries to emit a
``bo.model-observation.v1`` document through the existing telemetry adapter.

Hard guarantees:

* **Observe-only** — it never touches the request, the provider, or the
  chosen model; the return value of the call site is unchanged.
* **Fail-safe** — it never raises. A failure is logged and counted; the
  observation row, if already persisted, keeps ``delivered=0`` for retry.
* **No LLM dependency for BoBots** — BoBots never reach this hook (they
  make no provider calls), and ``bo.bots`` imports nothing from here.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openexecutive.bo.routing import engine, serialize, store
from openexecutive.bo.routing.engine import Policy, TaskContext

logger = logging.getLogger(__name__)


def _now_z() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _csv_setting(raw: Any) -> frozenset[str] | None:
    """CSV text setting → frozenset; empty string means *no restriction*."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _cost_cap(raw: Any) -> tuple[Decimal | None, str | None]:
    """``"<decimal> <CCY>"`` → (Decimal, currency); empty → (None, None)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    parts = raw.strip().split()
    if len(parts) != 2:
        return None, None
    try:
        return Decimal(parts[0]), parts[1]
    except InvalidOperation:
        return None, None


def load_policy(tenant: str, db_path: Path | None = None) -> tuple[Policy, str]:
    """Assemble the routing policy from ``bo.router.*`` tenant settings and
    return it with a deterministic ``policyVersion`` tag (content hash —
    identical configuration → identical version, which is what makes replay
    comparisons meaningful)."""
    from openexecutive.bo.settings import store as settings_store

    def val(key: str) -> Any:
        return settings_store.get_effective_value(tenant, key, db_path=db_path)

    cap, ccy = _cost_cap(val("bo.router.max_estimated_cost"))
    policy = Policy(
        allowed_providers=_csv_setting(val("bo.router.allowed_providers")),
        allowed_regions=_csv_setting(val("bo.router.allowed_regions")),
        required_capabilities=frozenset(
            _csv_setting(val("bo.router.required_capabilities")) or ()
        ),
        min_quality=int(val("bo.router.min_quality")) / 100.0,
        eval_max_age_days=int(val("bo.router.eval_max_age_days")),
        max_estimated_cost=cap,
        cost_currency=ccy,
    )
    material = {
        "allowed_providers": sorted(policy.allowed_providers or []),
        "allowed_regions": sorted(policy.allowed_regions or []),
        "required_capabilities": sorted(policy.required_capabilities),
        "min_quality": policy.min_quality,
        "eval_max_age_days": policy.eval_max_age_days,
        "max_estimated_cost": str(policy.max_estimated_cost)
        if policy.max_estimated_cost is not None
        else None,
        "cost_currency": policy.cost_currency,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return policy, f"pol_{digest}"


def _provider_name(model: str) -> str:
    """Which provider *would* serve this model — pure mirror of
    ``providers.registry.get_provider``'s routing rules, without building
    clients or ever raising."""
    try:
        from openexecutive.config import get_settings
        from openexecutive.providers import registry

        settings = get_settings()
        if model in registry._local_models(settings):  # noqa: SLF001
            return "local"
        if registry._is_claude(model):  # noqa: SLF001
            return "openrouter" if settings.openrouter_enabled else "anthropic"
        return "openrouter" if settings.openrouter_enabled else "unknown"
    except Exception:  # noqa: BLE001 — identity evidence must never break
        return "unknown"


def observe_call(
    *,
    model: str,
    actor: str,
    counts: dict[str, Any] | None,
    session_id: str | None = None,
    turn_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Record one routing observation for a completed model call.

    ``counts`` are the already-extracted usage counters from
    ``log_model_usage`` — the observation reuses them instead of re-reading
    the provider response.
    """
    try:
        from openexecutive.bo.identity import configured_tenant
        from openexecutive.bo.settings import store as settings_store

        tenant = configured_tenant()
        if not settings_store.get_effective_value(
            tenant, "bo.router.observe_enabled", db_path=db_path
        ):
            return None
    except Exception:  # noqa: BLE001 — never break the caller's call path
        return None

    obs_id: str | None = None
    try:
        if session_id is None or turn_id is None:
            try:
                from openexecutive.audit.context import get_active_ids

                ctx_session, ctx_turn = get_active_ids()
                session_id = session_id or ctx_session
                turn_id = turn_id or ctx_turn
            except Exception:  # noqa: BLE001
                pass
        correlation_id = turn_id or session_id or f"corr_{obs_id or 'anon'}"

        policy, policy_version = load_policy(tenant, db_path=db_path)
        catalog = store.list_catalog(tenant, db_path=db_path)
        catalog_version = f"cat_v{store.catalog_version(tenant, db_path=db_path)}"

        ctx = TaskContext(
            task_kind=actor,
            input_tokens=(counts or {}).get("input_tokens"),
            output_tokens=(counts or {}).get("output_tokens"),
        )
        decision = engine.recommend(catalog, policy, ctx)

        actual_route = {
            "provider": _provider_name(model),
            "modelId": model,
            "modelVersion": None,
        }
        measured = {
            "requests": 1,
            **(
                {"inputTokens": ctx.input_tokens}
                if ctx.input_tokens is not None
                else {}
            ),
            **(
                {"outputTokens": ctx.output_tokens}
                if ctx.output_tokens is not None
                else {}
            ),
        }
        billed = None
        cost_usd = (counts or {}).get("cost_usd")
        if cost_usd is not None:
            billed = {
                "amount": format(Decimal(str(cost_usd)).normalize(), "f"),
                "currency": "USD",
                "validUntil": _now_z()[:10],
                "evidenceRef": "provider:usage.cost",
            }

        occurred_at = _now_z()
        body = serialize.routing_body(
            correlation_id=correlation_id,
            policy_version=policy_version,
            catalog_version=catalog_version,
            task_kind=ctx.task_kind,
            recommendation=decision.recommendation,
            actual_route=actual_route,
            met_bar=decision.met_bar,
            reasons=decision.reasons,
            cost_estimate=decision.cost_estimate,
            measured=measured,
            billed=billed,
        )
        obs_id = store.record_observation(
            tenant,
            {
                "occurred_at": occurred_at,
                "correlation_id": correlation_id,
                "task_kind": ctx.task_kind,
                "actor_ref": actor,
                "policy_version": policy_version,
                "catalog_version": catalog_version,
                "decision": decision.decision,
                "met_bar": decision.met_bar,
                "reasons": decision.reasons,
                "recommendation": decision.recommendation,
                "actual_route": actual_route,
                "cost_estimate": decision.cost_estimate,
                "measured": measured,
                "billed": billed,
                "detail": decision.to_dict(),
                "event": body,
            },
            db_path=db_path,
        )

        # Deliver through the existing adapter — a lost receiver keeps the
        # row (delivered=0) for flush_pending; never raised to the caller.
        # ``None`` from emit means the adapter is disabled → dropped, not
        # delivered.
        try:
            from openexecutive.bo.telemetry.adapter import get_adapter

            sent = get_adapter().emit_model_observation(
                tenant=tenant, body=body, occurred_at=occurred_at
            )
            store.mark_delivered(
                tenant, obs_id,
                error=None if sent is not None else "telemetry disabled",
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001 — receiver may be down
            store.mark_delivered(
                tenant, obs_id, error=str(exc)[:200], db_path=db_path
            )
            logger.warning("observație de rutare nelivrată: %s", exc)

        retention = int(
            settings_store.get_effective_value(
                tenant, "bo.router.observation_retention_days", db_path=db_path
            )
        )
        store.sweep_observations(tenant, retention, db_path=db_path)
        return {"obs_id": obs_id, "decision": decision.decision}
    except Exception:  # noqa: BLE001 — observation is strictly best-effort
        logger.warning("bo.router.observe_call a eșuat", exc_info=True)
        return None


def flush_pending(tenant: str, db_path: Path | None = None) -> dict[str, int]:
    """Retry delivery of undelivered observations — the documented behaviour
    for 'Guardian indisponibil': rows persist locally, retried on demand."""
    from openexecutive.bo.telemetry.adapter import get_adapter

    adapter = get_adapter()
    sent = failed = 0
    for row in store.undelivered(tenant, db_path=db_path):
        try:
            sent_doc = adapter.emit_model_observation(
                tenant=tenant,
                body=row["event"],
                occurred_at=row["occurred_at"],
            )
            if sent_doc is None:
                raise RuntimeError("telemetria este dezactivată")
            store.mark_delivered(tenant, row["obs_id"], error=None, db_path=db_path)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            store.mark_delivered(
                tenant, row["obs_id"], error=str(exc)[:200], db_path=db_path
            )
            failed += 1
    return {"sent": sent, "failed": failed}


def emit_catalog_sync(tenant: str, db_path: Path | None = None) -> None:
    """Emit a ``models[]`` inventory event on catalog writes (best effort —
    a failed send never blocks the catalog write itself)."""
    from openexecutive.bo.telemetry.adapter import get_adapter

    try:
        entries = store.list_catalog(tenant, db_path=db_path)
        body = serialize.models_body(
            entries,
            owner_ref=f"tenant:{tenant}",
            last_seen=_now_z(),
            sync_id=f"sync_{store.catalog_version(tenant, db_path=db_path)}",
            complete=True,
        )
        get_adapter().emit_model_observation(tenant=tenant, body=body)
    except Exception:  # noqa: BLE001 — inventory sync is best-effort
        logger.warning("emit_catalog_sync a eșuat", exc_info=True)


__all__ = ["emit_catalog_sync", "flush_pending", "load_policy", "observe_call"]
