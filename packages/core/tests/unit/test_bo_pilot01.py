"""Synthetic HTTP pilot adverse tests; isolated files, no real providers."""
import json
import threading
from datetime import UTC, datetime, timedelta
from urllib.request import Request, urlopen

import pytest

from openexecutive.bo import db as database
from openexecutive.bo.execution import engine, guardian, store
from openexecutive.bo.execution.synth import ProviderTimeout, SyntheticCounterProvider
from openexecutive.bo.identity import ForbiddenError, Identity
from openexecutive.bo.packages import service as packages
from openexecutive.bo.packages import store as package_store
from openexecutive.bo.pilot import fixture, service
from openexecutive.bo.pilot.config import endpoint
from openexecutive.bo.pilot.provider import PilotProvider
from openexecutive.bo.settings import store as settings
from openexecutive.bo.settings.registry import SettingValidationError

from .bo_testkit import capture_audit, use_tmp_db


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    use_tmp_db(tmp_path, monkeypatch)
    service.initialize_db()
    capture_audit(monkeypatch)
    monkeypatch.setenv("BO_PACKAGES_DIR", str(tmp_path / "quarantine"))
    monkeypatch.setenv("BO_PILOT_SERVICE_TOKEN", "synthetic-token-a")
    for name in ("BO_TELEMETRY_ENDPOINT", "BO_GUARDIAN_TOKEN", "BO_GUARDIAN_POLICY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    httpd = fixture.server(tmp_path / "service.db", {"synthetic-token-a": "tenant-a", "synthetic-token-b": "tenant-b"})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    source = fixture.package(tmp_path)
    admin = Identity("synthetic-admin", "tenant-a", "admin", False, "proxy_email")
    def setting(key, value, tenant="tenant-a"):
        current = next(x for x in settings.list_effective(tenant) if x["key"] == key)
        return settings.set_value(tenant, key, value, expected_version=current["version"], actor="synthetic-admin")
    for key, value in {"bo.exec.enabled": True, "bo.packages.enabled": True,
        "bo.packages.trust_store_json": (tmp_path / "registry.json").read_text(),
        "bo.pilot.enabled": True, "bo.pilot.profile": "synthetic-loopback",
        "bo.pilot.endpoint": base, "bo.pilot.allowlist": json.dumps([base])}.items():
        setting(key, value)
    row = packages.import_package(admin, source)
    assert row["status"] == "QUARANTINED"
    packages.promote_to_draft(admin, row["id"])
    service.change_activation(admin, row["id"], True, 0, "Synthetic approval")
    mandate = store.create_mandate("tenant-a", {
        "allowed_resources": ["synth.erp"], "allowed_actions": ["diagnose"],
        "budget_limit": "100", "concurrency_limit": 2, "max_steps": 1, "max_depth": 2,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        parent=None, principal_ref="synthetic-admin", policy_version=1, actor="synthetic-admin", max_depth_cap=3)
    def call(path, body=None, token="synthetic-token-a"):
        with urlopen(Request(base + path, data=json.dumps(body).encode() if body else None,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}), timeout=2) as response:
            return json.load(response)
    yield {"admin": admin, "mandate": mandate, "source": source, "row": row,
           "setting": setting, "call": call, "tmp": tmp_path, "base": base}
    httpd.shutdown()
    httpd.server_close()
    thread.join(3)


def run(pilot):
    result = service.submit(pilot["admin"], pilot["mandate"].mandate_id)
    engine.work_once("tenant-a", provider=SyntheticCounterProvider(idempotent=True))
    return store.get_run("tenant-a", result["run_id"])


def test_healthy_and_delayed_are_evidence_not_zero(pilot):
    assert run(pilot)["state"] == "SUCCEEDED"
    assert service.status(pilot["admin"])["runs"][0]["health"] == "HEALTHY"
    pilot["call"]("/scenario", {"scenario": "delayed"})
    assert run(pilot)["state"] == "SUCCEEDED"
    status = service.status(pilot["admin"])["runs"][0]
    assert status["health"] == "STALE"
    assert status["observation"]["service"]["queuePending"] == 200
    import hashlib
    from pathlib import Path

    import jsonschema
    schema = Path(__file__).parent / "fixtures/bo.service-observation.v1.schema.json"
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == "a0421f3506aaecacd295b0f7c2c00594ff0f508cb06d712d9f5547a3566b9d27"
    jsonschema.validate(status["observation"], json.loads(schema.read_text()))


def test_unknown_resume_never_resubmits_then_readback(pilot):
    pilot["call"]("/scenario", {"scenario": "unknown"})
    result = run(pilot)
    assert result["state"] == "UNKNOWN"
    before = store.list_ledger("tenant-a", result["run_id"])[0]
    assert before["receipt_ref"] is None
    engine.resume_run("tenant-a", result["run_id"], actor="admin")
    engine.work_once("tenant-a", provider=SyntheticCounterProvider(idempotent=True))
    assert pilot["call"]("/stats") == {"effectCount": 1, "submitCalls": 1}
    assert store.get_run("tenant-a", result["run_id"])["state"] == "RECONCILIATION_REQUIRED"
    with pytest.raises(store.InvalidStateError):
        engine.reconcile_run("tenant-a", result["run_id"], SyntheticCounterProvider(idempotent=True), resolution="mark_failed", actor="admin")
    pilot["call"]("/scenario", {"scenario": "recover"})
    engine.reconcile_run("tenant-a", result["run_id"], SyntheticCounterProvider(idempotent=True), resolution="receipt", actor="admin")
    after = store.list_ledger("tenant-a", result["run_id"])[0]
    assert after["status"] == "SUCCEEDED" and after["receipt_ref"]
    assert (after["idempotency_key"], after["payload_digest"]) == (before["idempotency_key"], before["payload_digest"])
    assert pilot["call"]("/stats") == {"effectCount": 1, "submitCalls": 1}


@pytest.mark.parametrize("url", ["https://example.invalid", "http://localhost:8325", "http://127.0.0.1:80", "http://127.0.0.1:8325/", "http://x:secret@127.0.0.1:8325", "http://127.0.0.1:8325?x=1", "file:///tmp/x", "http://169.254.169.254:8325"])
def test_endpoint_rejects_unapproved_shapes(url):
    with pytest.raises(SettingValidationError):
        endpoint(url)


def test_activation_admin_cas_and_disable_before_claim(pilot):
    with pytest.raises(ForbiddenError):
        service.change_activation(Identity("svc", "tenant-a", "operator", True, "shared_secret"), pilot["row"]["id"], True, 1, "attempt")
    with pytest.raises(store.ConflictError):
        service.change_activation(pilot["admin"], pilot["row"]["id"], True, 0, "stale")
    result = service.submit(pilot["admin"], pilot["mandate"].mandate_id)
    service.change_activation(pilot["admin"], pilot["row"]["id"], False, 1, "Disable before dispatch")
    engine.work_once("tenant-a", provider=SyntheticCounterProvider(idempotent=True))
    assert store.get_run("tenant-a", result["run_id"])["state"] == "FAILED"
    assert pilot["call"]("/stats")["effectCount"] == 0


def test_changed_config_or_revoked_signer_blocks_dispatch(pilot):
    result = service.submit(pilot["admin"], pilot["mandate"].mandate_id)
    registry = json.loads((pilot["tmp"] / "registry.json").read_text())
    registry["keys"][0]["status"] = "revoked"
    pilot["setting"]("bo.packages.trust_store_json", json.dumps(registry))
    engine.work_once("tenant-a", provider=SyntheticCounterProvider(idempotent=True))
    assert store.get_run("tenant-a", result["run_id"])["state"] == "FAILED"
    assert pilot["call"]("/stats")["effectCount"] == 0


def test_required_authority_and_outage_not_telemetry(pilot, monkeypatch):
    pilot["setting"]("bo.pilot.supervision", "required")
    with pytest.raises(package_store.StateError):
        service.submit(pilot["admin"], pilot["mandate"].mandate_id)
    pilot["setting"]("bo.pilot.supervision", "standalone")
    def unavailable(*args, **kwargs):
        raise guardian.GuardianUnavailableError("synthetic outage")
    monkeypatch.setattr(guardian, "assert_effect_authorized", unavailable)
    assert run(pilot)["state"] == "PAUSED"
    assert pilot["call"]("/stats")["effectCount"] == 0


def test_two_tenants_isolated_and_wrong_service_credential_unknown(pilot, monkeypatch):
    assert run(pilot)["state"] == "SUCCEEDED"
    other = Identity("other", "tenant-b", "admin", False, "proxy_email")
    assert service.status(other)["runs"] == []
    assert service.activation("tenant-b") is None
    with pytest.raises(package_store.NotFoundError):
        service.verify_import("tenant-b", pilot["row"]["id"])
    assert pilot["call"]("/stats", token="synthetic-token-b")["effectCount"] == 0
    monkeypatch.setenv("BO_PILOT_SERVICE_TOKEN", "synthetic-token-b")
    assert run(pilot)["state"] == "FAILED"
    assert service.status(other)["runs"] == []
    assert pilot["call"]("/stats", token="synthetic-token-b")["effectCount"] == 0


@pytest.mark.parametrize("field", ["tenant", "effect_key", "digest", "receipt_ref"])
def test_receipt_correlation_required(pilot, field):
    result = run(pilot)
    entry = store.list_ledger("tenant-a", result["run_id"])[0]
    provider = PilotProvider("tenant-a", result)
    response = pilot["call"]("/receipts/" + entry["idempotency_key"])
    response["receipt"].pop(field)
    with pytest.raises(ProviderTimeout):
        provider._accept(response, entry["idempotency_key"], entry["payload_digest"])


def test_import_tampered_and_incompatible_never_activates(pilot):
    from openexecutive.bo.packages.registry import TrustRegistry
    from openexecutive.bo.packages.verify import verify_package
    registry = TrustRegistry.from_dict(json.loads((pilot["tmp"] / "registry.json").read_text()))
    assert not verify_package(pilot["source"], registry, tenant_ref="tenant-a", host_version="2.0.0").accepted
    (pilot["source"] / "bots/diagnostic.json").write_text("arbitrary script is data")
    assert not verify_package(pilot["source"], registry, tenant_ref="tenant-a").accepted


def test_migration_additive_preserves_prior_tables(pilot):
    result = run(pilot)
    before = store.list_ledger("tenant-a", result["run_id"])
    database.initialize_db()
    assert store.list_ledger("tenant-a", result["run_id"]) == before
    assert service.activation("tenant-a")["version"] == 1


def test_readback_after_deactivation_preserves_unknown_evidence(pilot):
    pilot["call"]("/scenario", {"scenario": "unknown"})
    result = run(pilot)
    service.change_activation(pilot["admin"], pilot["row"]["id"], False, 1, "Uninstall, keep evidence")
    pilot["call"]("/scenario", {"scenario": "recover"})
    outcome = engine.reconcile_run("tenant-a", result["run_id"], SyntheticCounterProvider(idempotent=True), resolution="receipt", actor="admin")
    assert outcome["resolved"] == 1
    assert pilot["call"]("/stats") == {"effectCount": 1, "submitCalls": 1}


def test_missing_secret_and_config_change_never_call(pilot, monkeypatch):
    monkeypatch.delenv("BO_PILOT_SERVICE_TOKEN")
    assert run(pilot)["state"] == "FAILED"
    monkeypatch.setenv("BO_PILOT_SERVICE_TOKEN", "synthetic-token-a")
    pending = service.submit(pilot["admin"], pilot["mandate"].mandate_id)
    pilot["setting"]("bo.pilot.timeout_s", 4)
    engine.work_once("tenant-a", provider=SyntheticCounterProvider(idempotent=True))
    assert store.get_run("tenant-a", pending["run_id"])["state"] == "FAILED"
    assert pilot["call"]("/stats")["submitCalls"] == 0


def test_service_outage_unknown_and_pending_cancel_pause_no_effect(pilot):
    pilot["setting"]("bo.exec.default_concurrency", 1)
    pilot["call"]("/scenario", {"scenario": "unavailable"})
    result = run(pilot)
    assert result["state"] == "UNKNOWN"
    assert pilot["call"]("/stats")["effectCount"] == 0
    assert service.status(pilot["admin"])["runs"][0]["health"] == "UNKNOWN"
    pilot["call"]("/scenario", {"scenario": "healthy"})
    for flag in ("pause", "cancel"):
        pending = service.submit(pilot["admin"], pilot["mandate"].mandate_id)
        store.request_flag("tenant-a", pending["run_id"], flag=flag + "_requested", actor="admin")
        engine.work_once("tenant-a", provider=SyntheticCounterProvider(idempotent=True))
        assert store.get_run("tenant-a", pending["run_id"])["state"] == ("PAUSED" if flag == "pause" else "CANCELLED")
    assert pilot["call"]("/stats")["effectCount"] == 0


def test_telemetry_outage_does_not_veto_correlated_receipt(pilot, monkeypatch):
    import sqlite3

    from openexecutive.bo.routing import store as outbox
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic telemetry outbox unavailable")
    monkeypatch.setattr(outbox, "enqueue_outbox", unavailable)
    result = run(pilot)
    assert result["state"] == "SUCCEEDED"
    entry = store.list_ledger("tenant-a", result["run_id"])[0]
    assert entry["status"] == "SUCCEEDED" and entry["receipt_ref"]
    assert entry["receipt"]["telemetryStatus"] == "DEGRADED"
    assert pilot["call"]("/stats") == {"effectCount": 1, "submitCalls": 1}


def test_telemetry_loss_does_not_relax_or_gate_standalone_authority(pilot, monkeypatch):
    from openexecutive.bo.pilot import delivery
    from openexecutive.bo.routing import store as outbox
    from openexecutive.bo.telemetry.adapter import TelemetryDisabledError
    monkeypatch.setattr(delivery, "get_adapter", lambda: type("Disabled", (), {"enabled": False})())
    assert run(pilot)["state"] == "SUCCEEDED"
    rows = outbox.claim_outbox("tenant-a", worker_id="probe", limit=50, lease_s=60)
    observation = next(r for r in rows if r["kind"] == "service")
    with pytest.raises(TelemetryDisabledError):
        delivery.deliver("tenant-a", observation["envelope"])
    assert pilot["call"]("/stats")["effectCount"] == 1


def test_redirect_is_not_followed(pilot):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Redirect(BaseHTTPRequestHandler):
        calls = 0
        def log_message(self, *args):
            pass
        def do_POST(self):
            Redirect.calls += 1
            self.send_response(307)
            self.send_header("Location", pilot["base"] + "/probe")
            self.end_headers()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        target = f"http://127.0.0.1:{httpd.server_port}"
        pilot["setting"]("bo.pilot.endpoint", target)
        pilot["setting"]("bo.pilot.allowlist", json.dumps([target]))
        assert run(pilot)["state"] == "FAILED"
        assert Redirect.calls == 1
        assert pilot["call"]("/stats")["effectCount"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(3)


def test_api_identity_settings_cas_and_activation(pilot, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.routes import bo
    from openexecutive.bo import identity
    monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "service-synthetic")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "proxy-synthetic")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@synthetic.invalid")
    monkeypatch.setattr(identity, "_is_principal_email", lambda _: False)
    app = FastAPI()
    app.include_router(bo.router)
    bo.register_error_handlers(app)
    client = TestClient(app)
    admin = {"x-api-key": "service-synthetic", "x-caller-email": "admin@synthetic.invalid", "x-caller-proxy-secret": "proxy-synthetic"}
    body = {"import_id": pilot["row"]["id"], "active": False, "expected_version": 1, "reason": "API deactivate"}
    assert client.put("/bo/pilot/activation", headers={"x-api-key": "service-synthetic"}, json=body).status_code == 403
    assert client.put("/bo/pilot/activation", headers={**admin, "x-caller-proxy-secret": "service-synthetic"}, json=body).status_code == 401
    assert client.get("/bo/pilot?tenant=tenant-b", headers=admin).status_code == 403
    assert client.put("/bo/pilot/activation", headers=admin, json=body).status_code == 200
    assert client.put("/bo/pilot/activation", headers=admin, json=body).status_code == 409
    assert client.put("/bo/settings/bo.pilot.stale_s", headers=admin, json={"value": 90, "expected_version": 0}).status_code == 200
    assert client.put("/bo/settings/bo.pilot.stale_s", headers=admin, json={"value": 95, "expected_version": 0}).status_code == 409
