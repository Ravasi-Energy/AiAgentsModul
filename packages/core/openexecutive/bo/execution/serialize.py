"""Serialization for ``bo.execution-control.v1`` execution events (VAL4-01).

The shared contract directory is owned by A02; until it publishes the
schema, these are the A01 domain examples the alignment is done against
(per the VAL4 decision document — "A01/A03 publish early examples from
their domain"). The shape follows the established bo.* conventions:

- envelope identity stamped at emission (eventId/producer/installation/
  tenant) — never trusted from the body;
- kinds: ``mandate``, ``run``, ``checkpoint``, ``receipt`` — checkpoint
  and receipt events carry REFERENCES and digests only, never payloads;
- delivery rides the same durable outbox as routing (kind="execution"),
  so a lost receiver never erases local evidence — retries resend the
  persisted envelope byte-identically.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = "bo.execution-control.v1"

_KINDS = frozenset({"mandate", "run", "checkpoint", "receipt", "policy"})


class ExecutionEventError(ValueError):
    """The event cannot be serialized for the wire."""


def build_execution_event(
    *,
    tenant: str,
    kind: str,
    body: dict[str, Any],
    occurred_at: str | None = None,
    producer_id: str = "boagents",
    installation_id: str = "local-installation",
) -> dict[str, Any]:
    """Assemble a complete envelope WITHOUT sending it — same contract as
    ``build_model_observation`` (REM-01): persist once, resend unchanged."""
    if kind not in _KINDS:
        raise ExecutionEventError(f"tip de eveniment necunoscut: {kind}")
    _assert_minimized(body)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "eventId": f"evt_{uuid.uuid4().hex[:24]}",
        "producerId": producer_id,
        "product": "BOAgents",
        "installationId": installation_id,
        "tenantRef": tenant,
        "occurredAt": occurred_at
        or datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "kind": kind,
        "body": body,
    }


def _assert_minimized(body: dict[str, Any]) -> None:
    """Fail closed on sensitive fields — execution events carry refs and
    digests, never payloads, secrets, principals or prompt content."""
    forbidden = {"payload", "secret", "token", "email", "prompt", "principal"}
    for key in body:
        lowered = key.lower()
        if any(bad in lowered for bad in forbidden):
            raise ExecutionEventError(
                f"câmp minimizat refuzat pe fir: {key}"
            )
    if len(str(body)) > 8192:
        raise ExecutionEventError("corpul evenimentului depășește 8 KiB")


def validate_execution_event(event: Any) -> dict[str, Any]:
    """Structural check mirroring the A02 contract conventions — replaced
    by schema validation once ``bo.execution-control.v1`` is published."""
    if not isinstance(event, dict):
        raise ExecutionEventError("evenimentul trebuie să fie un obiect")
    required = {
        "schemaVersion": SCHEMA_VERSION,
    }
    if event.get("schemaVersion") != required["schemaVersion"]:
        raise ExecutionEventError("schemaVersion invalid")
    for key in (
        "eventId", "producerId", "product", "installationId",
        "tenantRef", "occurredAt", "kind", "body",
    ):
        if key not in event or event[key] is None:
            raise ExecutionEventError(f"câmp obligatoriu lipsă: {key}")
    if event["kind"] not in _KINDS:
        raise ExecutionEventError(f"kind necunoscut: {event['kind']}")
    if not isinstance(event["body"], dict):
        raise ExecutionEventError("body trebuie să fie un obiect")
    _assert_minimized(event["body"])
    return event
