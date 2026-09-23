"""Verificarea unui pachet bo.package.v1 — implementarea de referință a
secțiunii 4 din CONTRACT-bo.package.v1.md.

Verificarea NU instalează nimic: citește directorul, confruntă manifestul,
semnătura și digesturile cu registrul de încredere și întoarce un verdict.
Nicio scriere, niciun efect extern.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bo_pkg.build import MANIFEST_NAME, sha256_file
from bo_pkg.canon import signed_payload
from bo_pkg.contract import (
    MANIFEST_MAX_BYTES,
    parse_semver,
    semver_cmp,
    validate_manifest,
)
from bo_pkg.errors import PackageReject
from bo_pkg.registry import TrustRegistry
from bo_pkg.signing import public_from_b64, verify


@dataclass
class Verdict:
    accepted: bool
    code: str = "ACCEPT"
    detail: str = ""
    checks: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.code if self.accepted else f"REJECT:{self.code}({self.detail})"


def _reject(code: str, detail: str, checks: list[str]) -> Verdict:
    return Verdict(False, code, detail, checks)


def verify_package(
    pkg_dir: Path,
    registry: TrustRegistry,
    *,
    installed: dict[str, str] | None = None,
    host_version: str = "1.0.0",
    now: datetime | None = None,
) -> Verdict:
    """Ordinea exactă din contract §4 — scurtcircuit la prima respingere."""
    now = now or datetime.now(timezone.utc)
    installed = installed or {}
    checks: list[str] = []

    # 1. manifest parse + schemă + limite
    mp = pkg_dir / MANIFEST_NAME
    try:
        raw = mp.read_bytes()
    except OSError:
        return _reject("INVALID_MANIFEST", "manifest.json lipsă", checks)
    if len(raw) > MANIFEST_MAX_BYTES:
        return _reject("INVALID_MANIFEST", "manifest peste 256 KiB", checks)

    def _no_const(x: str) -> None:
        raise ValueError(f"constantă JSON interzisă: {x}")

    try:
        doc: Any = json.loads(raw, parse_constant=_no_const)
    except ValueError:
        return _reject("INVALID_MANIFEST", "JSON invalid", checks)
    try:
        manifest = validate_manifest(doc)
    except PackageReject as e:
        return _reject(e.code, e.detail, checks)
    checks.append("manifest")

    # 2. publisher
    pub = registry.publisher(manifest["publisherId"])
    if pub is None:
        return _reject("PUBLISHER_UNKNOWN", manifest["publisherId"], checks)
    if pub["status"] != "active":
        return _reject("PUBLISHER_SUSPENDED", manifest["publisherId"], checks)
    checks.append("publisher")

    # 3. cheie
    key = registry.key(manifest["keyId"])
    if key is None or manifest["keyId"] not in pub["keyIds"]:
        return _reject("KEY_UNKNOWN", manifest["keyId"], checks)
    if key["status"] == "revoked":
        return _reject("KEY_REVOKED", manifest["keyId"], checks)
    if not registry.key_window_ok(key, now):
        return _reject("KEY_EXPIRED", manifest["keyId"], checks)
    checks.append("key")

    # 4. semnătură
    try:
        pk = public_from_b64(key["publicKey"])
    except PackageReject as e:
        return _reject(e.code, e.detail, checks)
    if not verify(pk, signed_payload(manifest), manifest["signature"]["value"]):
        return _reject("BAD_SIGNATURE", "semnătura nu verifică payload-ul canonic", checks)
    checks.append("signature")

    # 5. artefacte
    signed_paths = set(manifest["artifactDigests"])
    pkg_root = pkg_dir.resolve()
    for rel, declared in manifest["artifactDigests"].items():
        target = pkg_dir / rel
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            return _reject("ARTIFACT_MISSING", rel, checks)
        if not resolved.is_file():
            return _reject("ARTIFACT_MISSING", rel, checks)
        if not resolved.is_relative_to(pkg_root) or target.is_symlink():
            return _reject("TRAVERSAL", rel, checks)
        if sha256_file(resolved) != declared:
            return _reject("ARTIFACT_MODIFIED", rel, checks)
    on_disk = {
        p.relative_to(pkg_dir).as_posix()
        for p in pkg_dir.rglob("*") if p.is_file() and p.name != MANIFEST_NAME
    }
    extra = on_disk - signed_paths
    if extra:
        return _reject("UNSIGNED_ARTIFACT", sorted(extra)[0], checks)
    checks.append("artifacts")

    # 6. compatibilitate host
    compat = manifest["compatibility"]
    host = parse_semver(host_version)
    if compat["minHost"] and semver_cmp(host, parse_semver(compat["minHost"])) < 0:
        return _reject("INCOMPATIBLE", f"host {host_version} < {compat['minHost']}", checks)
    if compat["maxHost"] and semver_cmp(host, parse_semver(compat["maxHost"])) > 0:
        return _reject("INCOMPATIBLE", f"host {host_version} > {compat['maxHost']}", checks)
    checks.append("compatibility")

    # 7. capabilități
    catalog = set(registry.policy["capabilityCatalog"])
    for cap in manifest["requestedCapabilities"]:
        if cap not in catalog:
            return _reject("CAPABILITY_UNKNOWN", cap, checks)
    cap_limit = registry.max_caps_for(manifest["kind"])
    if cap_limit is not None and len(manifest["requestedCapabilities"]) > cap_limit:
        return _reject(
            "CAPABILITY_EXCESSIVE",
            f"{len(manifest['requestedCapabilities'])} > {cap_limit} pentru kind={manifest['kind']}",
            checks,
        )
    checks.append("capabilities")

    # 8. rollback
    current = installed.get(manifest["packageId"])
    if current is not None:
        if semver_cmp(parse_semver(manifest["version"]), parse_semver(current)) <= 0:
            if not (
                registry.policy["rollbackRequiresApproval"]
                and registry.rollback_approved(manifest["packageId"], manifest["version"])
            ):
                return _reject(
                    "ROLLBACK_UNAUTHORIZED",
                    f"{manifest['version']} <= instalat {current} fără aprobare",
                    checks,
                )
    checks.append("rollback")

    # 9. dimensiune totală
    total = sum((pkg_dir / rel).stat().st_size for rel in signed_paths)
    if total > registry.policy["maxPackageBytes"]:
        return _reject("PACKAGE_TOO_LARGE", f"{total} octeți", checks)
    checks.append("size")

    return Verdict(True, checks=checks)
