"""BO-TEL-001: bo.telemetry.v1 schema gate + injectable adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openexecutive.bo.settings import store as settings_store
from openexecutive.bo.settings.registry import SettingValidationError
from openexecutive.bo.telemetry import schema
from openexecutive.bo.telemetry.adapter import (
    BufferedTransport,
    HttpTransport,
    NullTransport,
    TelemetryAdapter,
    TelemetryDisabledError,
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
        lambda e: e.update({"configVersion": -1}),  # numeric: contract wants opaque string
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
    assert event["configVersion"] == "1"  # opaque string on the wire
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


# --------------------------------------------------------------------------- #
# S-02 — administered telemetry settings: tenant rows win over bootstrap,
# secrets stay env references, invalid input is refused at save time
# --------------------------------------------------------------------------- #

def test_tenant_override_wins_over_bootstrap(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    transport = BufferedTransport()
    adapter = TelemetryAdapter(enabled=False, transport=transport)

    rec = settings_store.set_value(
        "tenant-a", "bo.telemetry.enabled", True,
        expected_version=0, actor="admin@t",
    )
    assert rec["origin"] == "tenant" and rec["version"] == 1
    # CAS preserved on the new keys.
    with pytest.raises(settings_store.ConfigConflictError):
        settings_store.set_value(
            "tenant-a", "bo.telemetry.enabled", False,
            expected_version=0, actor="other@t",
        )

    cfg = adapter.resolve("tenant-a")
    assert cfg.enabled is True and cfg.source["enabled"] == "tenant"
    # A tenant without an override still sees the bootstrap flag.
    cfg_b = adapter.resolve("tenant-b")
    assert cfg_b.enabled is False and cfg_b.source["enabled"] == "bootstrap"

    # The administered enable actually reaches the wire at emit time.
    event = adapter.emit(
        tenant="tenant-a", kind="Heartbeat",
        data={"sequence": 1, "status": "HEALTHY",
              "observedAt": "2026-09-21T10:00:00Z"},
    )
    assert event is not None and transport.events == [event]
    # …and tenant-b remains governed by the disabled bootstrap.
    assert adapter.emit(
        tenant="tenant-b", kind="Heartbeat",
        data={"sequence": 1, "status": "HEALTHY",
              "observedAt": "2026-09-21T10:00:00Z"},
    ) is None


def test_administered_disable_overrides_enabled_bootstrap(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    adapter = TelemetryAdapter(enabled=True, transport=BufferedTransport())
    settings_store.set_value(
        "tenant-a", "bo.telemetry.enabled", False,
        expected_version=0, actor="admin@t",
    )
    with pytest.raises(TelemetryDisabledError):
        adapter.deliver_event({"eventId": "evt_1"}, tenant="tenant-a")
    assert adapter.emit(
        tenant="tenant-a", kind="Heartbeat",
        data={"sequence": 1, "status": "HEALTHY",
              "observedAt": "2026-09-21T10:00:00Z"},
    ) is None
    assert adapter.dropped == 2


def test_http_without_endpoint_and_token_is_controlled(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """transport=http saved without endpoint/token: the envelope is refused
    with a clear error and stays pending upstream — never silently dropped,
    never marked delivered, never a raw crash."""
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.delenv("BO_TELEMETRY_ENDPOINT", raising=False)
    monkeypatch.delenv("BO_TELEMETRY_TOKEN", raising=False)
    adapter = TelemetryAdapter(enabled=True, transport=BufferedTransport())
    settings_store.set_value(
        "tenant-a", "bo.telemetry.transport", "http",
        expected_version=0, actor="admin@t",
    )
    cfg = adapter.resolve("tenant-a")
    assert cfg.transport_kind == "http"
    assert cfg.transport is None and cfg.token_configured is False
    with pytest.raises(TelemetryDisabledError):
        adapter.deliver_event({"eventId": "evt_1"}, tenant="tenant-a")
    assert adapter.dropped == 1 and adapter.emitted == 0


def test_secret_ref_resolves_env_never_stores_secret(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """The administered token_ref names an env var; the secret value itself
    is never persisted in bo_settings and never surfaces in list_effective."""
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("TEL_TOKEN_X", "sekrit-token-material")
    settings_store.set_value(
        "tenant-a", "bo.telemetry.transport", "http",
        expected_version=0, actor="admin@t",
    )
    settings_store.set_value(
        "tenant-a", "bo.telemetry.endpoint", "https://guardian.example/v1/telemetry",
        expected_version=0, actor="admin@t",
    )
    settings_store.set_value(
        "tenant-a", "bo.telemetry.token_ref", "TEL_TOKEN_X",
        expected_version=0, actor="admin@t",
    )
    adapter = TelemetryAdapter(enabled=True)
    cfg = adapter.resolve("tenant-a")
    assert isinstance(cfg.transport, HttpTransport)
    assert cfg.transport.endpoint == "https://guardian.example/v1/telemetry"
    assert cfg.transport.token == "sekrit-token-material"
    assert cfg.token_ref == "TEL_TOKEN_X" and cfg.token_configured is True
    items = {i["key"]: i for i in settings_store.list_effective("tenant-a")}
    assert items["bo.telemetry.token_ref"]["value"] == "TEL_TOKEN_X"
    assert "sekrit" not in json.dumps(items)


def test_endpoint_override_gets_fresh_transport(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """An administered endpoint builds a NEW transport with the env-referenced
    token — the bootstrap token is never carried to a new destination."""
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("BO_TELEMETRY_TOKEN", "bootstrap-token")
    bootstrap = HttpTransport("https://env.example/t", "bootstrap-token")
    adapter = TelemetryAdapter(enabled=True, transport=bootstrap)

    cfg = adapter.resolve("tenant-a")
    assert cfg.transport is bootstrap  # unchanged config reuses bootstrap

    settings_store.set_value(
        "tenant-a", "bo.telemetry.endpoint", "https://admin.example/t",
        expected_version=0, actor="admin@t",
    )
    cfg = adapter.resolve("tenant-a")
    assert isinstance(cfg.transport, HttpTransport)
    assert cfg.transport is not bootstrap
    assert cfg.transport.endpoint == "https://admin.example/t"
    assert cfg.source["endpoint"] == "tenant"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("bo.telemetry.enabled", "yes"),                       # non-bool
        ("bo.telemetry.transport", "carrier-pigeon"),          # outside enum
        ("bo.telemetry.transport", "null"),                    # not administrable
        ("bo.telemetry.endpoint", "ftp://x"),                  # not http(s)
        ("bo.telemetry.endpoint", "has space"),                # invalid URL
        ("bo.telemetry.token_ref", "not a var name!"),         # not env syntax
        ("bo.telemetry.token_ref", "tok-with-D4sh.payload"),   # secret-shaped
    ],
)
def test_invalid_telemetry_settings_rejected(tmp_path, monkeypatch, key, value) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    with pytest.raises(SettingValidationError):
        settings_store.set_value("tenant-a", key, value,
                                 expected_version=0, actor="admin@t")
