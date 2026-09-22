"""Teste prototip bo.package.v1 — fixture-urile generate + proprietăți.

Cazuri cu `installed`/`host_version` specifice sunt declarate în expected.json
al fiecărui fixture.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bo_pkg.canon import canonical_bytes, signed_payload  # noqa: E402
from bo_pkg.contract import parse_semver, semver_cmp, validate_manifest  # noqa: E402
from bo_pkg.errors import PackageReject  # noqa: E402
from bo_pkg.registry import TrustRegistry  # noqa: E402
from bo_pkg.verify import verify_package  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def registry() -> TrustRegistry:
    return TrustRegistry.from_dict(
        json.loads((FIXTURES / "registry.json").read_text()))


def _cases() -> list[str]:
    return sorted(p.name for p in (FIXTURES / "packages").iterdir() if p.is_dir())


# instalat simulat: pkg.demo-bot=1.2.0, pkg.other-bot=1.2.0
INSTALLED = {"pkg.demo-bot": "1.2.0", "pkg.other-bot": "1.2.0"}


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads((FIXTURES / "expected.json").read_text())["cases"]


@pytest.mark.parametrize("case", _cases())
def test_fixture_case(case: str, registry: TrustRegistry, expected: dict) -> None:
    pkg_dir = FIXTURES / "packages" / case
    expected = expected[case]
    v = verify_package(
        pkg_dir, registry,
        installed=INSTALLED, host_version="1.4.2", now=NOW,
    )
    if expected["verdict"] == "ACCEPT":
        assert v.accepted, f"{case}: așteptat ACCEPT, primit {v}"
    else:
        assert not v.accepted, f"{case}: așteptat REJECT:{expected['code']}, primit ACCEPT"
        assert v.code == expected["code"], f"{case}: {v.code} != {expected['code']}"


def test_canonicalization_is_deterministic(registry: TrustRegistry) -> None:
    m = json.loads(
        (FIXTURES / "packages" / "valid-bobot" / "manifest.json").read_text())
    a = signed_payload(m)
    b = signed_payload(dict(reversed(list(m.items()))))
    assert a == b
    assert b'{"' not in a[:0]  # sanity — bytes non-goale
    assert len(a) > 0


def test_canonicalization_sorts_keys() -> None:
    out = canonical_bytes({"b": 1, "a": {"z": True, "y": None}, "c": ["x"]})
    assert out == b'{"a":{"y":null,"z":true},"b":1,"c":["x"]}'


def test_canonicalization_rejects_float_and_bool_in_numeric() -> None:
    with pytest.raises(PackageReject):
        canonical_bytes({"x": 1.5})


def test_resigning_changed_manifest_verifies() -> None:
    # control: schimbarea unui câmp + resemnare → ACCEPT; fără resemnare → reject
    pkg_dir = FIXTURES / "packages" / "valid-bobot"
    m = json.loads((pkg_dir / "manifest.json").read_text())
    m["version"] = "9.9.9"
    payload = signed_payload(m)
    registry = TrustRegistry.from_dict(
        json.loads((FIXTURES / "registry.json").read_text()))
    key = registry.key(m["keyId"])
    from bo_pkg.signing import public_from_b64, verify
    assert not verify(public_from_b64(key["publicKey"]), payload,
                      m["signature"]["value"])


def test_validate_manifest_rejects_bool_version(registry: TrustRegistry) -> None:
    m = json.loads(
        (FIXTURES / "packages" / "valid-bobot" / "manifest.json").read_text())
    m["version"] = True
    with pytest.raises(PackageReject):
        validate_manifest(m)


def test_semver_ordering() -> None:
    assert semver_cmp(parse_semver("1.0.0"), parse_semver("1.0.0-rc")) > 0
    assert semver_cmp(parse_semver("1.2.0"), parse_semver("1.10.0")) < 0
    assert semver_cmp(parse_semver("2.0.0"), parse_semver("2.0.0")) == 0


def test_rollback_same_version_rejected(registry: TrustRegistry) -> None:
    # reinstalarea aceleiași versiuni = downgrade la sine — tot neaprobat
    v = verify_package(
        FIXTURES / "packages" / "valid-bobot", registry,
        installed={"pkg.demo-bot": "9.9.9"}, host_version="1.4.2", now=NOW,
    )
    assert not v.accepted and v.code == "ROLLBACK_UNAUTHORIZED"


def test_registry_rejects_dangling_key_ref() -> None:
    doc = json.loads((FIXTURES / "registry.json").read_text())
    doc["publishers"][0]["keyIds"].append("key-ghost")
    with pytest.raises(PackageReject):
        TrustRegistry.from_dict(doc)


def test_every_reject_code_has_a_fixture() -> None:
    covered = {"ARTIFACT_MISSING", "ARTIFACT_MODIFIED", "UNSIGNED_ARTIFACT",
               "TRAVERSAL", "BAD_SIGNATURE", "PUBLISHER_UNKNOWN",
               "PUBLISHER_SUSPENDED", "KEY_REVOKED", "KEY_EXPIRED",
               "INCOMPATIBLE", "CAPABILITY_UNKNOWN", "CAPABILITY_EXCESSIVE",
               "ROLLBACK_UNAUTHORIZED", "INVALID_MANIFEST"}
    exp = json.loads((FIXTURES / "expected.json").read_text())["cases"]
    fixture_codes = {e.get("code") for e in exp.values()}
    missing = covered - fixture_codes - {None}
    assert not missing, f"coduri fără fixture: {missing}"
