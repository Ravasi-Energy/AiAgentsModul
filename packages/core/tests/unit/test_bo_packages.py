"""VAL2-01: import → verify → quarantine/draft pentru bo.package.v1.

Construiește pachete sintetice semnate Ed25519 în tmp dirs; acoperă toate
codurile de refuz ale deciziei de contract, idempotența/conflictul de versiune
și aprobările legate pentru downgrade.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openexecutive.bo.identity import Identity
from openexecutive.bo.packages import service, store
from openexecutive.bo.packages.canon import canonical_bytes, signed_payload
from openexecutive.bo.packages.errors import PackageReject
from openexecutive.bo.packages.verify import artifact_set_digest
from openexecutive.bo.settings import store as settings_store

from .bo_testkit import capture_audit, use_tmp_db

ADMIN = Identity(actor="admin@test", tenant="tenant-a", role="admin",
                 is_service=False, auth_source="proxy_email")
VIEWER = Identity(actor="view@test", tenant="tenant-a", role="viewer",
                  is_service=False, auth_source="proxy_email")
OTHER_TENANT = Identity(actor="admin@b", tenant="tenant-b", role="admin",
                        is_service=False, auth_source="proxy_email")

SK = Ed25519PrivateKey.generate()
PK_B64 = base64.b64encode(SK.public_key().public_bytes_raw()).decode()
SK2 = Ed25519PrivateKey.generate()
PK2_B64 = base64.b64encode(SK2.public_key().public_bytes_raw()).decode()


def _registry_doc(**over: Any) -> dict[str, Any]:
    doc = {
        "schemaVersion": "bo.package.registry.v1",
        "registryId": "reg-test",
        "version": "trust-1",
        "updatedAt": "2026-09-23T00:00:00Z",
        "publishers": [
            {"publisherId": "pub-a", "status": "active",
             "allowedKinds": ["bobot", "agent"], "keyIds": ["key-1", "key-old"]},
        ],
        "keys": [
            {"keyId": "key-1", "algorithm": "ed25519", "publicKey": PK_B64,
             "status": "active", "notBefore": "2026-01-01T00:00:00Z",
             "notAfter": None},
            {"keyId": "key-old", "algorithm": "ed25519", "publicKey": PK2_B64,
             "status": "revoked", "notBefore": "2026-01-01T00:00:00Z",
             "notAfter": None},
        ],
        "policy": {
            "policyVersion": "pol-1",
            "allowedKinds": ["bobot", "agent", "workflow"],
            "capabilityCatalog": ["bots:simulate", "bots:write",
                                  "settings:read", "notify:internal"],
            "maxCapabilitiesPerKind": {"bobot": 3, "agent": 8, "workflow": 8},
            "maxPackageBytes": 1 << 20,
            "rollbackRequiresApproval": True,
        },
    }
    doc.update(over)
    return doc


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return use_tmp_db(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    return capture_audit(monkeypatch)


@pytest.fixture()
def qroot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "quarantine"
    monkeypatch.setenv("BO_PACKAGES_DIR", str(root))
    return root


def _enable(db_path: Path, ident: Identity = ADMIN) -> None:
    settings_store.set_value(
        ident.tenant, "bo.packages.enabled", True,
        expected_version=0, actor=ident.actor, db_path=db_path)
    settings_store.set_value(
        ident.tenant, "bo.packages.trust_store_json",
        json.dumps(_registry_doc()),
        expected_version=0, actor=ident.actor, db_path=db_path)


def write_pkg(dest: Path, *, files: dict[str, bytes] | None = None,
              sk: Ed25519PrivateKey = SK, **m: Any) -> dict[str, Any]:
    files = files or {"bots/a.bobot.json": b'{"x":1}\n', "docs/r.md": b"doc\n"}
    dest.mkdir(parents=True, exist_ok=True)
    digests = {}
    for rel, content in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        digests[rel] = f"sha256:{hashlib.sha256(content).hexdigest()}"
    manifest: dict[str, Any] = {
        "schemaVersion": "bo.package.v1",
        "packageId": m.get("package_id", "pkg.demo"),
        "version": m.get("version", "1.0.0"),
        "kind": m.get("kind", "bobot"),
        "publisherId": m.get("publisher_id", "pub-a"),
        "keyId": m.get("key_id", "key-1"),
        "createdAt": m.get("created_at", "2026-09-23T00:00:00Z"),
        "artifactDigests": digests,
        "dependencies": m.get("dependencies", []),
        "compatibility": m.get("compatibility", {"minHost": None, "maxHost": None}),
        "requestedCapabilities": m.get("requested_capabilities", []),
        "signature": {"algorithm": "ed25519", "value": ""},
    }
    manifest["signature"]["value"] = base64.b64encode(
        sk.sign(signed_payload(manifest))).decode()
    (dest / "manifest.json").write_bytes(
        json.dumps(manifest, indent=2).encode())
    return manifest


def _import(ident: Identity, src: Path, db_path: Path) -> dict[str, Any]:
    return service.import_package(ident, src, db_path=db_path)


# -------------------------------------------------------------------------- #

class TestImportAccept:
    def test_valid_package_quarantined(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        write_pkg(src)
        res = _import(ADMIN, src, db_path)
        assert res["status"] == "QUARANTINED"
        assert res["verdict"]["verdict"] == "ACCEPT"
        assert res["verdict"]["tenantRef"] == "tenant-a"
        assert res["verdict"]["policyVersion"] == "pol-1"
        assert res["verdict"]["trustVersion"] == "trust-1"
        assert res["idempotent"] is False
        stored = Path(res["stored_path"])
        assert (stored / "bots/a.bobot.json").is_file()
        assert (stored / "manifest.json").is_file()

    def test_idempotent_reimport_same_digest(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        write_pkg(src)
        first = _import(ADMIN, src, db_path)
        second = _import(ADMIN, src, db_path)
        assert second["idempotent"] is True
        assert second["id"] == first["id"]
        assert len(store.list_imports("tenant-a", db_path=db_path)) == 1

    def test_same_version_different_digest_conflicts(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        _enable(db_path)
        write_pkg(tmp_path / "p1")
        write_pkg(tmp_path / "p2",
                  files={"bots/a.bobot.json": b'{"x":2}\n', "docs/r.md": b"doc\n"})
        assert _import(ADMIN, tmp_path / "p1", db_path)["status"] == "QUARANTINED"
        res = _import(ADMIN, tmp_path / "p2", db_path)
        assert res["verdict"]["verdict"] == "REJECT"
        assert res["verdict"]["reasons"][0].startswith("VERSION_CONFLICT")
        assert len(store.list_imports("tenant-a", db_path=db_path)) == 1

    def test_same_artifacts_different_manifest_is_conflict(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        """Un manifest modificat (ex. capabilități) peste același set de
        artefacte NU e idempotent — trebuie re-verificat, altfel verdictul
        vechi ar pretinde ACCEPT pentru conținut neverificat."""
        _enable(db_path)
        write_pkg(tmp_path / "p1")
        write_pkg(tmp_path / "p2", requested_capabilities=["bots:simulate"])
        assert _import(ADMIN, tmp_path / "p1", db_path)["status"] == "QUARANTINED"
        res = _import(ADMIN, tmp_path / "p2", db_path)
        assert res["verdict"]["verdict"] == "REJECT"
        assert res["verdict"]["reasons"][0].startswith("VERSION_CONFLICT")

    def test_rejected_row_does_not_block_valid_import(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        """Regresie: o respingere veche (aceeași versiune + același
        artifact-set) nu ocupă slotul unic — importul valid ulterior trebuie
        să treacă în carantină, nu să dea IntegrityError/500."""
        _enable(db_path)
        write_pkg(tmp_path / "bad", requested_capabilities=["net:egress"])
        write_pkg(tmp_path / "good")
        bad = _import(ADMIN, tmp_path / "bad", db_path)
        assert bad["status"] == "REJECTED"
        good = _import(ADMIN, tmp_path / "good", db_path)
        assert good["status"] == "QUARANTINED"
        # după ocuparea versiunii, orice manifest diferit (inclusiv cel
        # respins anterior) este VERSION_CONFLICT — nu se re-verifică
        bad2 = _import(ADMIN, tmp_path / "bad", db_path)
        assert bad2["verdict"]["verdict"] == "REJECT"
        assert bad2["verdict"]["reasons"][0].startswith("VERSION_CONFLICT")


class TestRejections:
    @pytest.mark.parametrize("mutation,code", [
        ("tamper", "ARTIFACT_MODIFIED"),
        ("extra", "UNSIGNED_ARTIFACT"),
        ("missing", "ARTIFACT_MISSING"),
        ("traversal", "TRAVERSAL"),
        ("bad_sig", "BAD_SIGNATURE"),
        ("revoked", "KEY_REVOKED"),
        ("unknown_pub", "PUBLISHER_UNKNOWN"),
        ("unknown_cap", "CAPABILITY_UNKNOWN"),
        ("excess_caps", "CAPABILITY_EXCESSIVE"),
        ("incompatible", "INCOMPATIBLE"),
        ("dup_key", "DUPLICATE_KEY"),
        ("nan_const", "INVALID_MANIFEST"),
        ("nested_manifest_unsigned", "UNSIGNED_ARTIFACT"),
    ])
    def test_reject_matrix(self, db_path: Path, qroot: Path, tmp_path: Path,
                           mutation: str, code: str) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        manifest = write_pkg(src)
        if mutation == "tamper":
            (src / "docs/r.md").write_bytes(b"schimbat dupa semnare\n")
        elif mutation == "extra":
            (src / "smuggled.py").write_bytes(b"import os\n")
        elif mutation == "missing":
            (src / "docs/r.md").unlink()
        elif mutation == "traversal":
            manifest["artifactDigests"]["../esc.txt"] = "sha256:" + "0" * 64
            manifest["signature"]["value"] = base64.b64encode(
                SK.sign(signed_payload(manifest))).decode()
            (src / "manifest.json").write_bytes(json.dumps(manifest).encode())
        elif mutation == "bad_sig":
            sig = base64.b64decode(manifest["signature"]["value"])
            manifest["signature"]["value"] = base64.b64encode(
                bytes([sig[0] ^ 1]) + sig[1:]).decode()
            (src / "manifest.json").write_bytes(json.dumps(manifest).encode())
        elif mutation == "revoked":
            write_pkg(src, key_id="key-old", sk=SK2)
        elif mutation == "unknown_pub":
            write_pkg(src, publisher_id="pub-x", key_id="key-1")
        elif mutation == "unknown_cap":
            write_pkg(src, requested_capabilities=["net:egress"])
        elif mutation == "excess_caps":
            write_pkg(src, requested_capabilities=[
                "bots:simulate", "bots:write", "settings:read", "notify:internal"])
        elif mutation == "incompatible":
            write_pkg(src, compatibility={"minHost": "99.0.0", "maxHost": None})
        elif mutation == "dup_key":
            raw = json.dumps(manifest).replace(
                '"packageId": "pkg.demo"',
                '"packageId": "pkg.demo", "packageId": "pkg.evil"')
            (src / "manifest.json").write_bytes(raw.encode())
        elif mutation == "nan_const":
            raw = json.dumps(manifest).replace('"version": "1.0.0"',
                                              '"version": NaN')
            (src / "manifest.json").write_bytes(raw.encode())
        elif mutation == "nested_manifest_unsigned":
            (src / "docs").mkdir(exist_ok=True)
            (src / "docs/manifest.json").write_bytes(b"{}\n")

        res = _import(ADMIN, src, db_path)
        assert res["verdict"]["verdict"] == "REJECT", f"{mutation}: {res}"
        assert res["verdict"]["reasons"][0].startswith(code), res["verdict"]
        if res.get("id"):
            assert res["status"] == "REJECTED"
            assert res["stored_path"] is None

    def test_nested_manifest_signed_is_accepted(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        write_pkg(src, files={"docs/manifest.json": b'{"nested":true}\n',
                              "bots/a.bobot.json": b'{"x":1}\n'})
        assert _import(ADMIN, src, db_path)["verdict"]["verdict"] == "ACCEPT"

    def test_packages_disabled(self, db_path: Path, tmp_path: Path) -> None:
        src = tmp_path / "pkg"
        write_pkg(src)
        with pytest.raises(PackageReject) as e:
            _import(ADMIN, src, db_path)
        assert e.value.code == "PACKAGES_DISABLED"

    def test_empty_trust_store_rejects(
            self, db_path: Path, tmp_path: Path) -> None:
        settings_store.set_value(
            "tenant-a", "bo.packages.enabled", True,
            expected_version=0, actor="a", db_path=db_path)
        src = tmp_path / "pkg"
        write_pkg(src)
        with pytest.raises(PackageReject) as e:
            _import(ADMIN, src, db_path)
        assert e.value.code == "INVALID_REGISTRY"

    def test_trust_store_accepts_pretty_json(
            self, db_path: Path, tmp_path: Path) -> None:
        # JSON indentat (cu \n/\t) e legal; restul caracterelor de control nu.
        settings_store.set_value(
            "tenant-a", "bo.packages.trust_store_json",
            json.dumps(_registry_doc(), indent=2),
            expected_version=0, actor="a", db_path=db_path)
        settings_store.set_value(
            "tenant-a", "bo.packages.enabled", True,
            expected_version=0, actor="a", db_path=db_path)
        src = tmp_path / "pkg"
        write_pkg(src)
        res = _import(ADMIN, src, db_path)
        assert res["verdict"]["verdict"] == "ACCEPT"
        from openexecutive.bo.settings.registry import SettingValidationError
        with pytest.raises(SettingValidationError):
            settings_store.set_value(
                "tenant-a", "bo.packages.trust_store_json", "{}\x01",
                expected_version=1, actor="a", db_path=db_path)

    def test_manifest_too_large(self, db_path: Path, tmp_path: Path) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        src.mkdir()
        (src / "manifest.json").write_bytes(b'{"a":"' + b"x" * (300 * 1024) + b'"}')
        res = _import(ADMIN, src, db_path)
        # limita e verificată înainte de parsare → respingere directă
        assert res["verdict"]["verdict"] == "REJECT"
        assert "MANIFEST_TOO_LARGE" in res["verdict"]["reasons"][0]


class TestDowngradeApprovals:
    def test_downgrade_needs_bound_approval(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        _enable(db_path)
        write_pkg(tmp_path / "v2", version="2.0.0")
        write_pkg(tmp_path / "v1", version="1.0.0")
        assert _import(ADMIN, tmp_path / "v2", db_path)["status"] == "QUARANTINED"
        res = _import(ADMIN, tmp_path / "v1", db_path)
        assert res["verdict"]["reasons"][0].startswith("ROLLBACK_UNAUTHORIZED")

    def test_downgrade_with_bound_approval_consumes_it(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        _enable(db_path)
        write_pkg(tmp_path / "v2", version="2.0.0")
        m1 = write_pkg(tmp_path / "v1", version="1.0.0")
        _import(ADMIN, tmp_path / "v2", db_path)
        asd = artifact_set_digest(m1["artifactDigests"])
        ap = service.create_approval(
            ADMIN, package_id="pkg.demo", from_version="2.0.0",
            to_version="1.0.0", artifact_set_digest_value=asd,
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            db_path=db_path)
        res = _import(ADMIN, tmp_path / "v1", db_path)
        assert res["status"] == "QUARANTINED"
        consumed = store.list_approvals("tenant-a", db_path=db_path)[0]
        assert consumed["consumed_at"] is not None
        # single-use: o a doua consumare directă e refuzată de store
        assert not store.consume_approval(
            "tenant-a", ap["id"], "imp_x", db_path=db_path)
        # reimportul acelorași octeți e idempotent (nu mai consuma aprobarea)
        res2 = _import(ADMIN, tmp_path / "v1", db_path)
        assert res2["idempotent"] is True

    def test_approval_wrong_digest_rejected(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        _enable(db_path)
        write_pkg(tmp_path / "v2", version="2.0.0")
        write_pkg(tmp_path / "v1", version="1.0.0")
        _import(ADMIN, tmp_path / "v2", db_path)
        service.create_approval(
            ADMIN, package_id="pkg.demo", from_version="2.0.0",
            to_version="1.0.0", artifact_set_digest_value="sha256:" + "f" * 64,
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            db_path=db_path)
        res = _import(ADMIN, tmp_path / "v1", db_path)
        assert res["verdict"]["reasons"][0].startswith("ROLLBACK_UNAUTHORIZED")

    def test_expired_approval_rejected(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        _enable(db_path)
        write_pkg(tmp_path / "v2", version="2.0.0")
        m1 = write_pkg(tmp_path / "v1", version="1.0.0")
        _import(ADMIN, tmp_path / "v2", db_path)
        asd = artifact_set_digest(m1["artifactDigests"])
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        store.insert_approval(
            {"tenant": "tenant-a", "package_id": "pkg.demo",
             "from_version": "2.0.0", "to_version": "1.0.0",
             "artifact_set_digest": asd, "expires_at": past,
             "created_by": "admin@test"}, db_path=db_path)
        res = _import(ADMIN, tmp_path / "v1", db_path)
        assert res["verdict"]["reasons"][0].startswith("ROLLBACK_UNAUTHORIZED")

    def test_other_tenant_approval_does_not_apply(
            self, db_path: Path, qroot: Path, tmp_path: Path) -> None:
        _enable(db_path)
        _enable(db_path, OTHER_TENANT)
        write_pkg(tmp_path / "v2", version="2.0.0")
        m1 = write_pkg(tmp_path / "v1", version="1.0.0")
        _import(ADMIN, tmp_path / "v2", db_path)
        asd = artifact_set_digest(m1["artifactDigests"])
        service.create_approval(
            OTHER_TENANT, package_id="pkg.demo", from_version="2.0.0",
            to_version="1.0.0", artifact_set_digest_value=asd,
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            db_path=db_path)
        res = _import(ADMIN, tmp_path / "v1", db_path)
        assert res["verdict"]["reasons"][0].startswith("ROLLBACK_UNAUTHORIZED")


class TestLifecycle:
    def test_promote_to_draft(self, db_path: Path, qroot: Path,
                              tmp_path: Path) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        write_pkg(src)
        res = _import(ADMIN, src, db_path)
        promoted = service.promote_to_draft(ADMIN, res["id"], db_path=db_path)
        assert promoted["status"] == "DRAFT"
        with pytest.raises(store.StateError):
            service.promote_to_draft(ADMIN, res["id"], db_path=db_path)

    def test_promote_detects_stored_drift(self, db_path: Path, qroot: Path,
                                         tmp_path: Path) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        write_pkg(src)
        res = _import(ADMIN, src, db_path)
        (Path(res["stored_path"]) / "docs/r.md").write_bytes(b"drift\n")
        with pytest.raises(PackageReject) as e:
            service.promote_to_draft(ADMIN, res["id"], db_path=db_path)
        assert e.value.code == "ARTIFACT_DRIFT"

    def test_copy_detects_drift_between_verify_and_import(
            self, db_path: Path, qroot: Path, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        write_pkg(src)
        real = service._copy_verified

        def _poison(s: Path, d: Path, digests: dict[str, str]) -> Path:
            (s / "docs/r.md").write_bytes(b"modificat intre verify si import\n")
            return real(s, d, digests)

        monkeypatch.setattr(service, "_copy_verified", _poison)
        res = _import(ADMIN, src, db_path)
        assert res["verdict"]["verdict"] == "REJECT"
        assert "ARTIFACT_DRIFT" in res["verdict"]["reasons"][0]


class TestCanon:
    def test_deterministic_sorted(self) -> None:
        out = canonical_bytes({"b": 1, "a": {"z": True, "y": None}, "c": ["x"]})
        assert out == b'{"a":{"y":null,"z":true},"b":1,"c":["x"]}'

    def test_unicode_roundtrip_and_escapes(self) -> None:
        out = canonical_bytes({"s": "țărișoră „BO” — emoji ✓"})
        assert "țărișoră".encode() in out  # UTF-8, nu \u escapare
        assert json.loads(out.decode())["s"].startswith("țărișoră")

    def test_float_rejected(self) -> None:
        with pytest.raises(PackageReject):
            canonical_bytes({"x": 1.5})


class TestAccess:
    def test_viewer_cannot_import(self, db_path: Path, qroot: Path,
                                  tmp_path: Path) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        write_pkg(src)
        from openexecutive.bo.identity import ForbiddenError
        with pytest.raises(ForbiddenError):
            _import(VIEWER, src, db_path)
        assert service.list_imports(VIEWER, db_path=db_path) is not None

    def test_tenant_isolation(self, db_path: Path, qroot: Path,
                              tmp_path: Path) -> None:
        _enable(db_path)
        src = tmp_path / "pkg"
        write_pkg(src)
        res = _import(ADMIN, src, db_path)
        with pytest.raises(store.NotFoundError):
            service.get_import(OTHER_TENANT, res["id"], db_path=db_path)
        assert service.list_imports(OTHER_TENANT, db_path=db_path) == []


# -------------------------------------------------------------------------- #
# HTTP surface
# -------------------------------------------------------------------------- #

class TestRoutes:
    @pytest.fixture()
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from openexecutive.api.routes import bo as bo_route

        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
        monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@test")
        monkeypatch.setenv("BO_PACKAGES_DIR", str(tmp_path / "quarantine"))
        capture_audit(monkeypatch)
        app = FastAPI()
        app.include_router(bo_route.router)
        bo_route.register_error_handlers(app)
        return TestClient(app)

    ADMIN_H = {"x-caller-email": "admin@test"}
    VIEWER_H = {"x-caller-email": "viewer@test"}

    def _enable_api(self, client) -> None:
        for key, val in [
            ("bo.packages.enabled", True),
            ("bo.packages.trust_store_json", json.dumps(_registry_doc())),
        ]:
            meta = client.get("/bo/settings", headers=self.ADMIN_H).json()
            ver = next(s["version"] for s in meta["settings"] if s["key"] == key)
            r = client.put(f"/bo/settings/{key}", headers=self.ADMIN_H,
                           json={"value": val, "expected_version": ver})
            assert r.status_code == 200, r.text

    def test_import_disabled_returns_422(self, client, tmp_path: Path) -> None:
        write_pkg(tmp_path / "pkg")
        r = client.post("/bo/packages/import", headers=self.ADMIN_H,
                        json={"source_dir": str(tmp_path / "pkg")})
        assert r.status_code == 422
        assert r.json()["error"] == "package_rejected"
        assert r.json()["detail"]["code"] == "PACKAGES_DISABLED"

    def test_viewer_cannot_import_but_can_list(
            self, client, tmp_path: Path) -> None:
        self._enable_api(client)
        write_pkg(tmp_path / "pkg")
        r = client.post("/bo/packages/import", headers=self.VIEWER_H,
                        json={"source_dir": str(tmp_path / "pkg")})
        assert r.status_code == 403
        r = client.get("/bo/packages", headers=self.VIEWER_H)
        assert r.status_code == 200

    def test_import_promote_flow(self, client, tmp_path: Path) -> None:
        self._enable_api(client)
        write_pkg(tmp_path / "pkg")
        r = client.post("/bo/packages/import", headers=self.ADMIN_H,
                        json={"source_dir": str(tmp_path / "pkg")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "QUARANTINED"
        assert body["verdict"]["verdict"] == "ACCEPT"
        imp_id = body["id"]
        r = client.post(f"/bo/packages/{imp_id}/promote", headers=self.ADMIN_H)
        assert r.status_code == 200
        assert r.json()["status"] == "DRAFT"
        # tenant mismatch → refuz 403 la nivel de identitate, înainte de store
        # (izolarea 404 la nivel de rând e acoperită de test_tenant_isolation)
        r = client.get(f"/bo/packages/{imp_id}",
                       headers={**self.ADMIN_H, "x-bo-tenant": "tenant-b"})
        assert r.status_code == 403

    def test_rejected_import_lists_verdict(
            self, client, tmp_path: Path) -> None:
        self._enable_api(client)
        write_pkg(tmp_path / "pkg")
        (tmp_path / "pkg" / "docs/r.md").write_bytes(b"modificat\n")
        r = client.post("/bo/packages/import", headers=self.ADMIN_H,
                        json={"source_dir": str(tmp_path / "pkg")})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "REJECTED"
        assert body["verdict"]["reasons"][0].startswith("ARTIFACT_MODIFIED")
        assert body["stored_path"] is None

    def test_approval_lifecycle_api(self, client, tmp_path: Path) -> None:
        self._enable_api(client)
        exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        r = client.post("/bo/packages-approvals", headers=self.ADMIN_H, json={
            "package_id": "pkg.demo", "from_version": "2.0.0",
            "to_version": "1.0.0", "artifact_set_digest": "sha256:" + "a" * 64,
            "expires_at": exp})
        assert r.status_code == 201, r.text
        ap_id = r.json()["id"]
        r = client.get("/bo/packages-approvals", headers=self.ADMIN_H)
        assert any(a["id"] == ap_id for a in r.json()["approvals"])
        r = client.post(f"/bo/packages-approvals/{ap_id}/revoke",
                        headers=self.ADMIN_H)
        assert r.status_code == 200
        # aprobare expirată → 422
        r = client.post("/bo/packages-approvals", headers=self.ADMIN_H, json={
            "package_id": "pkg.demo", "from_version": "2.0.0",
            "to_version": "1.0.0", "artifact_set_digest": "sha256:" + "a" * 64,
            "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()})
        assert r.status_code == 422
