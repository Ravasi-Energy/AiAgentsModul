"""BO-TEL-001: bo.telemetry.v1 schema gate + injectable adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openexecutive.bo.settings import store as settings_store
from openexecutive.bo.telemetry import schema
from openexecutive.bo.telemetry.adapter import (
    BufferedTransport,
    NullTransport,
    TelemetryAdapter,
    set_adapter,
)
from openexecutive.bo.telemetry.schema import TelemetrySchemaError

from .bo_testkit import capture_audit, use_tmp_db

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures/bo/telemetry"


@pytest.fixture(autouse=True)
def _reset_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_audit(monkeypatch)  # set_value inside these tests must not leak
    set_adapter(None)
    yield
    set_adapter(None)


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# Fixture contract: the schema accepts ONLY the agreed shape
# --------------------------------------------------------------------------- #

def test_all_valid_fixtures_accepted() -> None:
    valid = sorted((FIXTURES / "valid").glob("*.json"))
    assert len(valid) == 5  # one per kind
    for path in valid:
        event = _load(path)
        assert schema.validate_event(event) is event, path.name


def test_all_invalid_fixtures_rejected() -> None:
    invalid = sorted((FIXTURES / "invalid").glob("*.json"))
    assert len(invalid) >= 7
    for path in invalid:
        with pytest.raises(TelemetrySchemaError):
            schema.validate_event(_load(path))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update({"tenantRef": ""}),
        lambda e: e.update({"eventId": "with space"}),
        lambda e: e.update({"configVersion": -1}),
        lambda e: e["data"].update({"sequence": -1}),
        lambda e: e["data"].update({"note": "camp extra"}),
        lambda e: e.pop("correlationId"),
    ],
)
def test_schema_mutation_rejected(mutate) -> None:  # noqa: ANN001
    event = _load(FIXTURES / "valid/heartbeat.json")
    mutate(event)
    with pytest.raises(TelemetrySchemaError):
        schema.validate_event(event)


# --------------------------------------------------------------------------- #
# Adapter: disabled by default, injectable transport
# --------------------------------------------------------------------------- #

def test_disabled_adapter_drops_without_io(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    transport = BufferedTransport()
    adapter = TelemetryAdapter(enabled=False, transport=transport)
    out = adapter.emit(tenant="tenant-a", kind="Heartbeat",
                       data={"sequence": 1, "status": "HEALTHY",
                             "observedAt": "2026-09-21T10:00:00Z"})
    assert out is None
    assert adapter.dropped == 1
    assert adapter.emitted == 0
    assert transport.events == []  # nothing reached the wire


def test_enabled_adapter_stamps_and_sends(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    transport = BufferedTransport()
    adapter = TelemetryAdapter(enabled=True, transport=transport,
                               producer_id="boagents",
                               installation_id="install-test")
    settings_store.set_value("tenant-a", "bo.ui.language", "en",
                             expected_version=0, actor="t")
    event = adapter.emit(tenant="tenant-a", kind="Heartbeat",
                         data={"sequence": 7, "status": "DEGRADED",
                               "observedAt": "2026-09-21T10:00:00Z"})
    assert event is not None
    # Identity is stamped at emission — product/tenant/installation come from
    # the adapter + caller tenant, never from caller-supplied fields.
    assert event["product"] == "BOAgents"
    assert event["tenantRef"] == "tenant-a"
    assert event["installationId"] == "install-test"
    assert event["schemaVersion"] == "bo.telemetry.v1"
    assert event["configVersion"] == 1
    schema.validate_event(event)  # the emitted envelope is itself valid
    assert transport.events == [event]
    assert adapter.emitted == 1


def test_adapter_rejects_malformed_emission(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    adapter = TelemetryAdapter(enabled=True, transport=BufferedTransport())
    with pytest.raises(TelemetrySchemaError):
        adapter.emit(tenant="tenant-a", kind="Heartbeat",
                     data={"sequence": "NaN", "status": "NOPE",
                           "observedAt": "not-a-time"})
    assert adapter.rejected == 1
    assert adapter.emitted == 0


def test_null_transport_is_silent() -> None:
    adapter = TelemetryAdapter(enabled=True, transport=NullTransport())
    # Emits (validates + sends to null) without error.
    assert adapter.emit(tenant="t", kind="Heartbeat",
                        data={"sequence": 0, "status": "HEALTHY",
                              "observedAt": "2026-09-21T10:00:00Z"}) is not None
