"""``bo.telemetry.v1`` event validation — CONTRACTE-V1 + decizia VAL1-02.

Strict, closed-world validation: every field is required or explicitly
nullable, unknown fields are rejected (the schema accepts *only* the agreed
shape — "fixture acceptă numai schema convenită"), enums are exact, numbers
reject bool, and timestamps must carry an explicit UTC zone. The canonical
JSON Schema artifact ships at
``fixtures/bo/telemetry/bo.telemetry.v1.schema.json``; this module is the
runtime gate and stays in lockstep via the fixture tests.

VAL1-02 contract decisions encoded here:
- ``configVersion`` is an opaque non-empty string (envelope + ConfigApplied);
  the internal numeric settings version is serialized explicitly at emit.
- ``correlationId`` + ``configVersion`` required; ``agentRef``/``runRef``
  nullable; ``runRef`` required non-empty for run events.
- The run reference lives in the envelope — never duplicated inside data.
- RunStarted.data ⊆ {trigger, definitionRef, versionNo, runKind}, all
  optional, no user input.
- RunFinished.data requires executionStatus + verificationStatus; optional
  planHash (sha256) and finite durationMs >= 0.
- VerificationFinding requires ownerRef, severity in
  INFO/LOW/MEDIUM/HIGH/CRITICAL, evidenceRefs list (max 32 refs x 128 chars).
- ConfigApplied.data requires key, configVersion, applyMode, appliedVersion
  (nullable); actorRef is an opaque server-derived ref, never an email.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "bo.telemetry.v1"

PRODUCTS = ("BOAgents", "Hire")
KINDS = ("Heartbeat", "RunStarted", "RunFinished", "VerificationFinding",
         "ConfigApplied")
HEARTBEAT_STATUS = ("HEALTHY", "DEGRADED", "UNAVAILABLE")
EXECUTION_STATUS = ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "UNKNOWN")
VERIFICATION_STATUS = ("VERIFIED", "PENDING", "MISMATCH", "UNKNOWN")
EFFECT_STATUS = ("NOT_EXECUTED", "EXECUTED", "PARTIAL", "UNKNOWN")
SEVERITY = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
APPLY_MODE = ("IMMEDIATE", "NEW_RUN", "RESTART", "MIGRATION")
RUN_KINDS = ("RunStarted", "RunFinished")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$"
)


class TelemetrySchemaError(ValueError):
    pass


def _err(msg: str) -> None:
    raise TelemetrySchemaError(msg)


def _check_id(v: Any, field: str) -> None:
    if not isinstance(v, str) or not _ID_RE.match(v):
        _err(f"{field}: id opac invalid")


def _check_ts(v: Any, field: str) -> None:
    if not isinstance(v, str) or not _RFC3339_RE.match(v):
        _err(f"{field}: așteptat timestamp RFC3339 cu fus UTC explicit")
    else:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            _err(f"{field}: timestamp imposibil")


def _check_opaque(v: Any, field: str, *, max_len: int = 128) -> None:
    """Opaque non-empty string (contract: configVersion, opaque refs)."""
    if not isinstance(v, str) or not v or len(v) > max_len:
        _err(f"{field}: referință opacă invalidă")


def _check_str(v: Any, field: str, *, nullable: bool = False, max_len: int = 512) -> None:
    if v is None and nullable:
        return
    if not isinstance(v, str) or not v or len(v) > max_len:
        _err(f"{field}: string invalid")


def _check_enum(v: Any, field: str, allowed: tuple[str, ...]) -> None:
    if v not in allowed:
        _err(f"{field}: valoare nepermisă {v!r}; permise: {allowed}")


_ENVELOPE_FIELDS = {
    "schemaVersion", "eventId", "producerId", "installationId", "tenantRef",
    "product", "kind", "occurredAt", "correlationId", "agentRef", "runRef",
    "configVersion", "data",
}

_DATA_FIELDS: dict[str, set[str]] = {
    "Heartbeat": {"sequence", "status", "observedAt"},
    "RunStarted": {"trigger", "definitionRef", "versionNo", "runKind"},
    "RunFinished": {"executionStatus", "verificationStatus",
                    "planHash", "durationMs"},
    "VerificationFinding": {"findingId", "category", "severity",
                            "effectStatus", "ownerRef", "evidenceRefs"},
    "ConfigApplied": {"key", "configVersion", "applyMode", "appliedVersion",
                      "actorRef"},
}

_DATA_REQUIRED: dict[str, set[str]] = {
    "Heartbeat": {"sequence", "status", "observedAt"},
    "RunStarted": set(),
    "RunFinished": {"executionStatus", "verificationStatus"},
    "VerificationFinding": {"findingId", "category", "severity",
                            "effectStatus", "ownerRef"},
    "ConfigApplied": {"key", "configVersion", "applyMode", "appliedVersion"},
}

# Field-name denylist inside `data` — the default payload must never carry
# prompts, documents, secrets, or personal data (CONTRACTE-V1). Belt-and-
# suspenders under the closed-field rule above.
_DATA_DENIED_NAMES = {
    "prompt", "prompts", "document", "documents", "secret", "password",
    "token", "email", "phone", "cnp", "ssn", "address", "payload",
}


def _validate_data(kind: str, data: Any) -> None:
    if not isinstance(data, dict):
        _err("data: așteptat obiect")
    allowed = _DATA_FIELDS[kind]
    required = _DATA_REQUIRED[kind]
    unknown = set(data) - allowed
    if unknown:
        _err(f"data.{kind}: câmpuri nepermise {sorted(unknown)}")
    missing = required - set(data)
    if missing:
        _err(f"data.{kind}: lipsesc {sorted(missing)}")
    for name in data:
        if name.lower() in _DATA_DENIED_NAMES:
            _err(f"data.{kind}: câmp interzis {name!r}")

    if kind == "Heartbeat":
        seq = data["sequence"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            _err("Heartbeat.sequence: întreg >= 0")
        _check_enum(data["status"], "Heartbeat.status", HEARTBEAT_STATUS)
        _check_ts(data["observedAt"], "Heartbeat.observedAt")
    elif kind == "RunStarted":
        if "trigger" in data:
            _check_str(data["trigger"], "RunStarted.trigger", max_len=64)
        if "definitionRef" in data:
            _check_id(data["definitionRef"], "RunStarted.definitionRef")
        if "versionNo" in data:
            vn = data["versionNo"]
            if not isinstance(vn, int) or isinstance(vn, bool) or vn < 1:
                _err("RunStarted.versionNo: întreg >= 1")
        if "runKind" in data:
            _check_str(data["runKind"], "RunStarted.runKind", max_len=64)
    elif kind == "RunFinished":
        _check_enum(data["executionStatus"], "executionStatus", EXECUTION_STATUS)
        _check_enum(data["verificationStatus"], "verificationStatus",
                    VERIFICATION_STATUS)
        if "planHash" in data:
            ph = data["planHash"]
            if not isinstance(ph, str) or not re.fullmatch(r"[0-9a-f]{64}", ph):
                _err("RunFinished.planHash: sha256 hex")
        if "durationMs" in data:
            d = data["durationMs"]
            if (not isinstance(d, (int, float)) or isinstance(d, bool)
                    or not math.isfinite(d) or d < 0):
                _err("RunFinished.durationMs: număr finit >= 0")
    elif kind == "VerificationFinding":
        _check_id(data["findingId"], "findingId")
        _check_str(data["category"], "category", max_len=64)
        _check_enum(data["severity"], "severity", SEVERITY)
        _check_enum(data["effectStatus"], "effectStatus", EFFECT_STATUS)
        _check_opaque(data["ownerRef"], "ownerRef", max_len=256)
        if "evidenceRefs" in data:
            ev = data["evidenceRefs"]
            if not isinstance(ev, list) or len(ev) > 32:
                _err("evidenceRefs: listă max 32 referințe")
            for ref in ev:
                _check_opaque(ref, "evidenceRefs[]", max_len=128)
    elif kind == "ConfigApplied":
        _check_str(data["key"], "key", max_len=128)
        _check_opaque(data["configVersion"], "ConfigApplied.configVersion")
        _check_enum(data["applyMode"], "applyMode", APPLY_MODE)
        av = data["appliedVersion"]
        if av is not None and (
                not isinstance(av, int) or isinstance(av, bool) or av < 0):
            _err("ConfigApplied.appliedVersion: întreg >= 0 sau null")
        if "actorRef" in data:
            ref = data["actorRef"]
            _check_str(ref, "actorRef", max_len=256)
            if "@" in ref:
                _err("actorRef: trebuie referință opacă, nu email")


def validate_event(event: Any) -> dict[str, Any]:
    """Validate one telemetry event; returns it unchanged on success."""
    if not isinstance(event, dict):
        _err("evenimentul trebuie să fie un obiect")
    unknown = set(event) - _ENVELOPE_FIELDS
    if unknown:
        _err(f"câmpuri necunoscute în envelopă: {sorted(unknown)}")
    for required in ("schemaVersion", "eventId", "producerId",
                     "installationId", "tenantRef", "product", "kind",
                     "occurredAt", "correlationId", "data"):
        if required not in event:
            _err(f"lipsește {required}")

    if event["schemaVersion"] != SCHEMA_VERSION:
        _err(f"schemaVersion invalid: {event['schemaVersion']!r}")
    for f in ("eventId", "producerId", "installationId", "tenantRef",
              "correlationId"):
        _check_id(event[f], f)
    _check_enum(event["product"], "product", PRODUCTS)
    _check_enum(event["kind"], "kind", KINDS)
    _check_ts(event["occurredAt"], "occurredAt")
    for required in ("configVersion",):
        if required not in event:
            _err(f"lipsește {required}")
    _check_opaque(event["configVersion"], "configVersion")
    if "agentRef" in event and event["agentRef"] is not None:
        _check_id(event["agentRef"], "agentRef")
    if "runRef" in event and event["runRef"] is not None:
        _check_id(event["runRef"], "runRef")
    if event["kind"] in RUN_KINDS and not event.get("runRef"):
        _err(f"{event['kind']}: runRef obligatoriu nevid în envelopă")
    _validate_data(event["kind"], event["data"])
    return event
