"""bo.package.v1 verification — contract §4 order, fail-fast, drift-aware.

Limits are enforced before and *during* reads: per-file size is checked from
stat before opening, and the running total aborts mid-stream the moment the
package budget is exceeded. A nested `manifest.json` is an ordinary artifact —
only the root manifest is exempt from the unsigned-file check.

`verify_package` performs no writes and no external effects. It returns a
`Verdict` carrying every field the coordinator decision requires for the
verifier contract (A02 consumes the same shape).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.bo.packages.canon import signed_payload
from openexecutive.bo.packages.contract import (
    MANIFEST_NAME,
    load_manifest,
    parse_semver,
    semver_cmp,
    validate_manifest,
)
from openexecutive.bo.packages.errors import PackageReject
from openexecutive.bo.packages.registry import TrustRegistry
from openexecutive.bo.packages.signing import public_from_b64, verify

_CHUNK = 64 * 1024
VERDICT_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class Approval:
    """Tenant-bound downgrade approval. An arbitrary approvalRef is NOT
    authority — the record must match tenant+package+from+to+digest, be
    unexpired and unconsumed. Stored in bo_package_approvals (store.py)."""
    approval_id: str
    tenant_ref: str
    package_id: str
    from_version: str
    to_version: str
    artifact_set_digest: str
    expires_at: datetime
    consumed: bool = False
    status: str = "active"  # active | revoked

    def matches(self, *, tenant_ref: str, package_id: str, from_version: str,
                to_version: str, artifact_set_digest: str,
                now: datetime) -> bool:
        return (
            self.status == "active"
            and not self.consumed
            and self.tenant_ref == tenant_ref
            and self.package_id == package_id
            and self.from_version == from_version
            and self.to_version == to_version
            and self.artifact_set_digest == artifact_set_digest
            and now.astimezone(UTC) <= self.expires_at.astimezone(UTC)
        )


@dataclass
class Verdict:
    """Verifier output — the shared verdict shape of the contract decision."""
    accepted: bool
    code: str = "ACCEPT"
    detail: str = ""
    checks: list[str] = field(default_factory=list)
    package_id: str | None = None
    version: str | None = None
    manifest_digest: str | None = None
    artifact_set_digest: str | None = None
    tenant_ref: str | None = None
    publisher_id: str | None = None
    key_id: str | None = None
    policy_version: str | None = None
    trust_version: str | None = None
    checked_at: str | None = None
    expires_at: str | None = None
    # set when a matching downgrade approval was found; the importer consumes it
    approval_id: str | None = None
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": "ACCEPT" if self.accepted else "REJECT",
            "reasons": [] if self.accepted else [f"{self.code}: {self.detail}"],
            "packageId": self.package_id,
            "version": self.version,
            "manifestDigest": self.manifest_digest,
            "artifactSetDigest": self.artifact_set_digest,
            "tenantRef": self.tenant_ref,
            "publisherId": self.publisher_id,
            "keyId": self.key_id,
            "policyVersion": self.policy_version,
            "trustVersion": self.trust_version,
            "checkedAt": self.checked_at,
            "expiresAt": self.expires_at,
            "idempotent": self.idempotent,
        }


def artifact_set_digest(digests: dict[str, str]) -> str:
    """Deterministic digest over the whole signed artifact set."""
    items = sorted(digests.items())
    blob = json.dumps(items, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(blob).hexdigest()}"


def _reject(code: str, detail: str, checks: list[str], **kw: Any) -> Verdict:
    return Verdict(False, code, detail, checks, **kw)


def _hash_file_limited(path: Path, budget: int) -> tuple[str, int]:
    """Stream-hash `path`; abort mid-read if `budget` bytes are exceeded."""
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > budget:
                raise PackageReject(
                    "PACKAGE_TOO_LARGE", f"artifact {path.name} exceeds budget")
            h.update(chunk)
    return f"sha256:{h.hexdigest()}", total


def verify_package(
    pkg_dir: Path,
    registry: TrustRegistry,
    *,
    tenant_ref: str,
    installed: dict[str, str] | None = None,
    approvals: Iterable[Approval] = (),
    host_version: str = "1.0.0",
    now: datetime | None = None,
    verdict_ttl: timedelta = VERDICT_TTL,
) -> Verdict:
    """Contract §4 order — short-circuit at the first rejection."""
    now = now or datetime.now(UTC)
    installed = installed or {}
    checks: list[str] = []
    base: dict[str, Any] = {
        "tenant_ref": tenant_ref,
        "policy_version": registry.policy["policyVersion"],
        "trust_version": registry.version,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + verdict_ttl).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # 1. manifest parse (size cap before+read-time, dup keys, constants) + schema
    try:
        manifest = validate_manifest(load_manifest(pkg_dir / MANIFEST_NAME))
    except PackageReject as e:
        return _reject(e.code, e.detail, checks, **base)
    base.update(
        package_id=manifest["packageId"], version=manifest["version"],
        publisher_id=manifest["publisherId"], key_id=manifest["keyId"],
        manifest_digest=f"sha256:{hashlib.sha256(signed_payload(manifest)).hexdigest()}",
        artifact_set_digest=artifact_set_digest(manifest["artifactDigests"]),
    )
    checks.append("manifest")

    # 2. publisher
    pub = registry.publisher(manifest["publisherId"])
    if pub is None:
        return _reject("PUBLISHER_UNKNOWN", manifest["publisherId"], checks, **base)
    if pub["status"] != "active":
        return _reject("PUBLISHER_SUSPENDED", manifest["publisherId"], checks, **base)
    checks.append("publisher")

    # 3. key
    key = registry.key(manifest["keyId"])
    if key is None or manifest["keyId"] not in pub["keyIds"]:
        return _reject("KEY_UNKNOWN", manifest["keyId"], checks, **base)
    if key["status"] == "revoked":
        return _reject("KEY_REVOKED", manifest["keyId"], checks, **base)
    if not registry.key_window_ok(key, now):
        return _reject("KEY_EXPIRED", manifest["keyId"], checks, **base)
    checks.append("key")

    # 4. signature
    try:
        pk = public_from_b64(key["publicKey"])
    except PackageReject as e:
        return _reject(e.code, e.detail, checks, **base)
    if not verify(pk, signed_payload(manifest), manifest["signature"]["value"]):
        return _reject("BAD_SIGNATURE", "signature does not verify", checks, **base)
    checks.append("signature")

    # 5. artifacts — size budget checked per-file (stat, before open) and
    #    incrementally during the read; only the ROOT manifest is exempt from
    #    the unsigned-file inventory.
    max_bytes = registry.policy["maxPackageBytes"]
    signed_paths = set(manifest["artifactDigests"])
    pkg_root = pkg_dir.resolve()
    total = 0
    for rel, declared in manifest["artifactDigests"].items():
        target = pkg_dir / rel
        try:
            st = target.stat()
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            return _reject("ARTIFACT_MISSING", rel, checks, **base)
        if not resolved.is_file():
            return _reject("ARTIFACT_MISSING", rel, checks, **base)
        if not resolved.is_relative_to(pkg_root) or target.is_symlink():
            return _reject("TRAVERSAL", rel, checks, **base)
        if st.st_size > max_bytes - total:
            return _reject(
                "PACKAGE_TOO_LARGE", f"{rel}: {st.st_size}B over remaining budget",
                checks, **base)
        try:
            actual, n = _hash_file_limited(resolved, max_bytes - total)
        except PackageReject as e:
            return _reject(e.code, e.detail, checks, **base)
        total += n
        if actual != declared:
            return _reject("ARTIFACT_MODIFIED", rel, checks, **base)
    on_disk = {
        p.relative_to(pkg_dir).as_posix()
        for p in pkg_dir.rglob("*")
        if p.is_file() and p.relative_to(pkg_dir).as_posix() != MANIFEST_NAME
    }
    extra = on_disk - signed_paths
    if extra:
        return _reject("UNSIGNED_ARTIFACT", sorted(extra)[0], checks, **base)
    checks.append("artifacts")

    # 6. host compatibility — against the consuming product's runtime version
    compat = manifest["compatibility"]
    host = parse_semver(host_version)
    if compat["minHost"] and semver_cmp(host, parse_semver(compat["minHost"])) < 0:
        return _reject(
            "INCOMPATIBLE", f"host {host_version} < {compat['minHost']}",
            checks, **base)
    if compat["maxHost"] and semver_cmp(host, parse_semver(compat["maxHost"])) > 0:
        return _reject(
            "INCOMPATIBLE", f"host {host_version} > {compat['maxHost']}",
            checks, **base)
    checks.append("compatibility")

    # 7. capabilities
    catalog = set(registry.policy["capabilityCatalog"])
    for cap in manifest["requestedCapabilities"]:
        if cap not in catalog:
            return _reject("CAPABILITY_UNKNOWN", cap, checks, **base)
    cap_limit = registry.max_caps_for(manifest["kind"])
    if cap_limit is not None and len(manifest["requestedCapabilities"]) > cap_limit:
        return _reject(
            "CAPABILITY_EXCESSIVE",
            f"{len(manifest['requestedCapabilities'])} > {cap_limit} for kind={manifest['kind']}",
            checks, **base)
    checks.append("capabilities")

    # 8. version semantics: same version + same digest = idempotent; same
    #    version + different digest = conflict; downgrade = bound approval.
    current = installed.get(manifest["packageId"])
    if current is not None:
        cmp_ = semver_cmp(parse_semver(manifest["version"]), parse_semver(current))
        if cmp_ == 0:
            return _reject(
                "VERSION_CONFLICT",
                f"{manifest['version']} already present with a different digest",
                checks, **base)
        if cmp_ < 0 and registry.policy["rollbackRequiresApproval"]:
            asd = base["artifact_set_digest"]
            match = next(
                (a for a in approvals if a.matches(
                    tenant_ref=tenant_ref, package_id=manifest["packageId"],
                    from_version=current, to_version=manifest["version"],
                    artifact_set_digest=asd, now=now)),
                None,
            )
            if match is None:
                return _reject(
                    "ROLLBACK_UNAUTHORIZED",
                    f"{manifest['version']} < installed {current} without a bound approval",
                    checks, **base)
            base["approval_id"] = match.approval_id
    checks.append("versioning")

    return Verdict(True, checks=checks, **base)
