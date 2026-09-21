"""BOAgents routes — Settings slice, deterministic BoBots, telemetry.

Valul 1 surface (mandate BO-A01). All paths are tenant-scoped: the tenant is
resolved from server-side configuration + authenticated identity
(``bo.identity``), never from request payloads. Roles: viewer < operator <
admin — writes are admin-only, simulations are operator+.

Domain exceptions are translated centrally in ``api.main`` (see
``_bo_error_handlers``): NotFound→404, CAS/state conflicts→409, validation→
422, identity failures→401/403 — and a scope denial never discloses what
exists behind it (CONTRACTE-V1).

Nothing under ``/bo`` can reach an LLM provider or produce an external
effect: simulation receipts are ``NOT_EXECUTED`` by construction, and the
telemetry adapter is disabled unless explicitly configured.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from openexecutive.bo import identity as bo_identity
from openexecutive.bo.bots import examples as bot_examples
from openexecutive.bo.bots import service as bot_service
from openexecutive.bo.bots import store as bot_store
from openexecutive.bo.settings import store as settings_store
from openexecutive.bo.settings.registry import SettingValidationError
from openexecutive.bo.telemetry.adapter import get_adapter
from openexecutive.bo.telemetry.schema import TelemetrySchemaError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bo")


# --------------------------------------------------------------------------- #
# Error mapping — registered on the app by api.main (and by tests that mount
# this router standalone). Scope denials intentionally disclose nothing about
# the protected data.
# --------------------------------------------------------------------------- #

def _bo_json(status_code: int, error: str, detail: object = None) -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(body, status_code=status_code)


async def _bo_identity_exc(_req: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, bo_identity.IdentityError)
    return _bo_json(exc.status_code, exc.error, str(exc))


async def _bo_not_found(_req: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, settings_store.UnknownSettingError):
        return _bo_json(404, "unknown_setting", str(exc))
    return _bo_json(404, "not_found", str(exc))


async def _bo_conflict(_req: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, bot_store.StateError):
        return _bo_json(409, "state_conflict", str(exc))
    return _bo_json(409, "version_conflict", str(exc))


async def _bo_invalid(_req: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, bot_service.ValidationFailure):
        return _bo_json(422, "invalid_definition", exc.errors)
    if isinstance(exc, bot_service.SimulationRefused):
        return _bo_json(422, "simulation_refused", str(exc))
    if isinstance(exc, TelemetrySchemaError):
        return _bo_json(422, "invalid_telemetry", str(exc))
    return _bo_json(422, "invalid_value", str(exc))


def register_error_handlers(app: FastAPI) -> None:
    """Map BO domain exceptions to HTTP responses on ``app``."""
    app.add_exception_handler(bo_identity.IdentityError, _bo_identity_exc)  # type: ignore[arg-type]
    app.add_exception_handler(bot_store.NotFoundError, _bo_not_found)  # type: ignore[arg-type]
    app.add_exception_handler(settings_store.UnknownSettingError, _bo_not_found)  # type: ignore[arg-type]
    app.add_exception_handler(bot_store.ConflictError, _bo_conflict)  # type: ignore[arg-type]
    app.add_exception_handler(settings_store.ConfigConflictError, _bo_conflict)  # type: ignore[arg-type]
    app.add_exception_handler(bot_store.StateError, _bo_conflict)  # type: ignore[arg-type]
    app.add_exception_handler(bot_service.ValidationFailure, _bo_invalid)  # type: ignore[arg-type]
    app.add_exception_handler(bot_service.SimulationRefused, _bo_invalid)  # type: ignore[arg-type]
    app.add_exception_handler(SettingValidationError, _bo_invalid)  # type: ignore[arg-type]
    app.add_exception_handler(TelemetrySchemaError, _bo_invalid)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Identity + request bodies
# --------------------------------------------------------------------------- #

def _identity(request: Request) -> bo_identity.Identity:
    return bo_identity.resolve_identity(request)


BoIdentity = Annotated[bo_identity.Identity, Depends(_identity)]


class _SettingPatch(BaseModel):
    value: Any
    expected_version: int = Field(ge=0)


class _BotCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "BOT"
    description: str = Field(default="", max_length=500)
    content: dict[str, Any]


class _BotPatch(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    content: dict[str, Any] | None = None


class _SimulateRequest(BaseModel):
    input: dict[str, Any] | None = None
    version_no: int | None = Field(default=None, ge=1)
    draft: bool = False


# --------------------------------------------------------------------------- #
# Settings (BO-SET-001)
# --------------------------------------------------------------------------- #

@router.get("/settings")
def list_settings(ident: BoIdentity) -> Any:
    bo_identity.require(ident, "settings:read")
    return {
        "tenant": ident.tenant,
        "config_version": settings_store.config_version(ident.tenant),
        "settings": settings_store.list_effective(ident.tenant),
        "role": ident.role,
    }


@router.put("/settings/{key}")
def put_setting(key: str, body: _SettingPatch,
                ident: BoIdentity) -> Any:
    bo_identity.require(ident, "settings:write")
    record = settings_store.set_value(
        ident.tenant, key, body.value,
        expected_version=body.expected_version, actor=ident.actor,
    )
    _config_applied(ident, key, record)
    return {
        "result": "SAVED",
        "apply_mode": record["apply_mode"],
        # SAVED vs APPLIED are distinct per contract: an IMMEDIATE setting is
        # live at once; NEW_RUN applies to simulations started after save.
        "applied": record["apply_mode"] == "IMMEDIATE",
        "setting": record,
    }


def _config_applied(ident: bo_identity.Identity, key: str,
                    record: dict[str, Any]) -> None:
    """Emit ConfigApplied for IMMEDIATE settings — through the adapter, so it
    is a no-op unless telemetry is explicitly enabled."""
    if record["apply_mode"] != "IMMEDIATE":
        return
    try:
        get_adapter().emit(tenant=ident.tenant, kind="ConfigApplied", data={
            "key": key, "configVersion": record["version"],
            "applyMode": "IMMEDIATE", "actorRef": ident.actor,
        })
    except Exception:  # noqa: BLE001 — telemetry never breaks the write path
        logger.warning("ConfigApplied emit failed", exc_info=True)


# --------------------------------------------------------------------------- #
# BoBots (BO-BOT-001/002/003)
# --------------------------------------------------------------------------- #

@router.get("/bots")
def list_bots(ident: BoIdentity) -> Any:
    bo_identity.require(ident, "bots:read")
    return {"bots": bot_store.list_definitions(ident.tenant)}


@router.post("/bots", status_code=201)
def create_bot(body: _BotCreate,
               ident: BoIdentity) -> Any:
    bo_identity.require(ident, "bots:write")
    definition = bot_service.create(
        ident.tenant, ident.actor, body.model_dump())
    return {"bot": definition}


@router.post("/bots/example", status_code=201)
def create_example_bot(ident: BoIdentity) -> Any:
    """Install the mandated synthetic example: stale-heartbeat → finding."""
    bo_identity.require(ident, "bots:write")
    definition = bot_service.create(
        ident.tenant, ident.actor, dict(bot_examples.HEARTBEAT_STALE))
    return {"bot": definition}


@router.get("/bots/{def_id}")
def get_bot(def_id: str,
            ident: BoIdentity) -> Any:
    bo_identity.require(ident, "bots:read")
    definition = bot_store.get_definition(ident.tenant, def_id)
    versions = bot_store.list_versions(ident.tenant, def_id)
    draft = bot_store.get_version(ident.tenant, def_id, status="draft")
    active = (
        bot_store.get_version(
            ident.tenant, def_id, version_no=definition["active_version_no"])
        if definition["active_version_no"] is not None else None
    )
    return {
        "bot": definition,
        "versions": versions,
        "draft": draft,
        "active": active,
    }


@router.patch("/bots/{def_id}")
def patch_bot(def_id: str, body: _BotPatch,
              ident: BoIdentity) -> Any:
    bo_identity.require(ident, "bots:write")
    definition = bot_service.update_draft(
        ident.tenant, ident.actor, def_id, body.model_dump(),
    )
    return {"bot": definition, "result": "SAVED"}


@router.post("/bots/{def_id}/publish")
def publish_bot(def_id: str,
                ident: BoIdentity) -> Any:
    bo_identity.require(ident, "bots:write")
    definition = bot_service.publish(ident.tenant, ident.actor, def_id)
    return {"bot": definition, "result": "APPLIED",
            "active_version_no": definition["active_version_no"]}


@router.post("/bots/{def_id}/simulate", status_code=201)
def simulate_bot(def_id: str, body: _SimulateRequest,
                 ident: BoIdentity) -> Any:
    """Deterministic dry-run. Persists the run + step timeline + plan hash."""
    bo_identity.require(ident, "bots:simulate")
    run = bot_service.simulate(
        ident.tenant, ident.actor, def_id,
        input_context=body.input,
        version_no=body.version_no,
        simulate_draft=body.draft,
    )
    return {"run": run}


@router.get("/bots/{def_id}/runs")
def list_bot_runs(def_id: str,
                  ident: BoIdentity) -> Any:
    bo_identity.require(ident, "bots:read")
    runs = bot_store.list_runs(ident.tenant, def_id)
    return {"runs": runs}


@router.get("/runs/{run_id}")
def get_run(run_id: str,
            ident: BoIdentity) -> Any:
    bo_identity.require(ident, "bots:read")
    return {"run": bot_store.get_run(ident.tenant, run_id)}


# --------------------------------------------------------------------------- #
# Telemetry (BO-TEL-001)
# --------------------------------------------------------------------------- #

@router.get("/telemetry/status")
def telemetry_status(ident: BoIdentity) -> Any:
    bo_identity.require(ident, "telemetry:read")
    adapter = get_adapter()
    return {
        "enabled": adapter.enabled,
        "transport": type(adapter.transport).__name__,
        "emitted": adapter.emitted,
        "dropped": adapter.dropped,
        "rejected": adapter.rejected,
        "schema_version": "bo.telemetry.v1",
        "note": "Telemetria este oprită implicit; se activează doar prin "
                "configurație explicită (BO_TELEMETRY_*).",
    }


@router.post("/telemetry/validate")
def telemetry_validate(ident: BoIdentity,
                       payload: Annotated[Any, Body()] = None) -> Any:
    """Schema gate: accepts only a well-formed ``bo.telemetry.v1`` event."""
    bo_identity.require(ident, "telemetry:read")
    get_adapter().validate_incoming(payload)
    return {"ok": True}
