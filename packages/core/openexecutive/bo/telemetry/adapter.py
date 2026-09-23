"""BOAgents telemetry adapter — injectable, disabled by default.

Contract (BO-TEL-001): the adapter emits ``bo.telemetry.v1`` envelopes through
a pluggable transport. With ``enabled=false`` (the default) every ``emit`` is
dropped before it is even built — zero I/O, zero network. No transport is
ever created implicitly: operators inject one via env config
(``BO_TELEMETRY_*``) or construct the adapter directly in tests.

Identity is stamped at emission time (``tenantRef`` = the authenticated
tenant, ``product`` = "BOAgents", ``producerId``/``installationId`` from
deployment config) — never trusted from caller-supplied payload.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from openexecutive.bo.settings import store as settings_store
from openexecutive.bo.telemetry import schema

logger = logging.getLogger(__name__)

_PRODUCT = "BOAgents"


def opaque_actor_ref(actor: str) -> str:
    """Server-derived opaque ref for actorRef/ownerRef — never the email.

    Deterministic (same actor -> same ref) so findings can be correlated,
    but not reversible and carries no personal data on the wire.
    """
    digest = hashlib.sha256(actor.encode("utf-8")).hexdigest()[:20]
    return f"actor_{digest}"


class Transport(Protocol):
    def send(self, event: dict[str, Any]) -> None: ...


class NullTransport:
    """Accepts and discards — used when telemetry is enabled-but-unrouted."""

    def send(self, event: dict[str, Any]) -> None:  # noqa: ARG002
        return


class BufferedTransport:
    """Keeps the last ``capacity`` events in memory — tests and UI preview."""

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = capacity
        self.events: list[dict[str, Any]] = []

    def send(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if len(self.events) > self.capacity:
            del self.events[: len(self.events) - self.capacity]


class HttpTransport:
    """POST the event as JSON to a Guardian-compatible ingestion endpoint.

    Exists for contract completeness; it is *never* instantiated unless an
    operator explicitly configures ``BO_TELEMETRY_TRANSPORT=http`` together
    with endpoint + token. Tests use BufferedTransport.
    """

    def __init__(self, endpoint: str, token: str, timeout_s: float = 5.0) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout_s = timeout_s

    def send(self, event: dict[str, Any]) -> None:
        import urllib.request

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(event).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=self.timeout_s).read()  # noqa: S310


class TelemetryAdapter:
    """Builds + validates + routes ``bo.telemetry.v1`` events."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        transport: Transport | None = None,
        producer_id: str = "boagents",
        installation_id: str = "local-installation",
    ) -> None:
        self.enabled = enabled
        self.transport = transport or NullTransport()
        self.producer_id = producer_id
        self.installation_id = installation_id
        self.emitted = 0
        self.dropped = 0
        self.rejected = 0

    def emit(
        self,
        *,
        tenant: str,
        kind: str,
        data: dict[str, Any],
        agent_ref: str | None = None,
        run_ref: str | None = None,
        correlation_id: str | None = None,
        db_path: Path | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            self.dropped += 1
            return None
        try:
            config_version = settings_store.config_version(tenant, db_path=db_path)
        except Exception:  # noqa: BLE001 — telemetry must survive a settings
            config_version = 0   # store hiccup; it never gates the product
        # Contract: configVersion is an opaque non-empty string on the wire;
        # the internal numeric version is serialized explicitly here.
        config_version_str = str(config_version)
        event = {
            "schemaVersion": schema.SCHEMA_VERSION,
            "eventId": f"evt_{uuid.uuid4().hex[:24]}",
            "producerId": self.producer_id,
            "installationId": self.installation_id,
            "tenantRef": tenant,
            "product": _PRODUCT,
            "kind": kind,
            "occurredAt": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "correlationId": correlation_id or f"corr_{uuid.uuid4().hex[:24]}",
            "agentRef": agent_ref,
            "runRef": run_ref,
            "configVersion": config_version_str,
            "data": data,
        }
        try:
            schema.validate_event(event)
        except schema.TelemetrySchemaError:
            self.rejected += 1
            raise
        self.transport.send(event)
        self.emitted += 1
        return event

    def validate_incoming(self, event: Any) -> dict[str, Any]:
        """Strictly validate an externally-supplied event (fixture endpoint)."""
        return schema.validate_event(event)

    def emit_model_observation(
        self,
        *,
        tenant: str,
        body: dict[str, Any],
        occurred_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Emit a ``bo.model-observation.v1`` document (VAL3-01, A02 contract).

        Same transport + identity stamping as ``emit``: the caller supplies
        only the body members (``models`` and/or ``routing``); envelope fields
        are server-derived. Disabled adapter → dropped before the document is
        even assembled for the wire (``self.dropped`` counts it).
        """
        if not self.enabled:
            self.dropped += 1
            return None
        event = {
            "schemaVersion": "bo.model-observation.v1",
            "eventId": f"evt_{uuid.uuid4().hex[:24]}",
            "producerId": self.producer_id,
            "product": _PRODUCT,
            "installationId": self.installation_id,
            "tenantRef": tenant,
            "observedAt": occurred_at
            or datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            **body,
        }
        self.transport.send(event)
        self.emitted += 1
        return event


_adapter: TelemetryAdapter | None = None


def _build_from_env() -> TelemetryAdapter:
    enabled = os.environ.get("BO_TELEMETRY_ENABLED", "").lower() in (
        "1", "true", "yes",
    )
    transport: Transport = NullTransport()
    if enabled:
        kind = os.environ.get("BO_TELEMETRY_TRANSPORT", "buffered")
        if kind == "http":
            endpoint = os.environ.get("BO_TELEMETRY_ENDPOINT", "")
            token = os.environ.get("BO_TELEMETRY_TOKEN", "")
            if not endpoint or not token:
                logger.warning(
                    "BO_TELEMETRY_TRANSPORT=http fără endpoint/token — "
                    "telemetria rămâne dezactivată"
                )
                return TelemetryAdapter(enabled=False)
            transport = HttpTransport(endpoint, token)
        elif kind == "buffered":
            transport = BufferedTransport()
    return TelemetryAdapter(
        enabled=enabled,
        transport=transport,
        producer_id=os.environ.get("BO_TELEMETRY_PRODUCER_ID", "boagents"),
        installation_id=os.environ.get("BO_INSTALLATION_ID", "local-installation"),
    )


def get_adapter() -> TelemetryAdapter:
    global _adapter
    if _adapter is None:
        _adapter = _build_from_env()
    return _adapter


def set_adapter(adapter: TelemetryAdapter | None) -> None:
    """Replace the process adapter — tests inject BufferedTransport here."""
    global _adapter
    _adapter = adapter
