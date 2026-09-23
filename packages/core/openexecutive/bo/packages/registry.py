"""bo.package.registry.v1 — tenant trust store: publishers, keys, policy.

Authority comes only from this store — never from the package. The registry
document is JSON; in product it is held in the `bo.packages.trust_store_json`
setting (admin-owned, audited via the settings CAS/audit path). Downgrade
approvals are NOT part of the trust store (contract §3) — they are per-tenant
records passed to the verifier as context.

`version` and `policy.policyVersion` are REQUIRED declared labels; they are
bound verbatim into every verdict as `trustVersion`/`policyVersion`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from openexecutive.bo.packages.contract import OPAQUE_ID
from openexecutive.bo.packages.errors import PackageReject

REGISTRY_SCHEMA_VERSION = "bo.package.registry.v1"
ALGORITHMS = {"ed25519"}


def _require_tz_aware(ts: Any, field_name: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        raise PackageReject("INVALID_REGISTRY", f"{field_name} is not RFC3339") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise PackageReject(
            "INVALID_REGISTRY", f"{field_name} lacks explicit timezone")
    return dt


def _str_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise PackageReject("INVALID_REGISTRY", f"{field_name}: string list expected")


class TrustRegistry:
    def __init__(
        self,
        registry_id: str,
        version: str,
        publishers: dict[str, dict[str, Any]],
        keys: dict[str, dict[str, Any]],
        policy: dict[str, Any],
    ) -> None:
        self.registry_id = registry_id
        # declared `version` label — bound verbatim as verdict trustVersion
        self.version = version
        self.publishers = publishers
        self.keys = keys
        self.policy = policy

    @classmethod
    def from_dict(cls, doc: Any) -> TrustRegistry:
        if not isinstance(doc, dict) or doc.get("schemaVersion") != REGISTRY_SCHEMA_VERSION:
            raise PackageReject("INVALID_REGISTRY", "wrong schemaVersion")
        if set(doc) - {"schemaVersion", "registryId", "version", "updatedAt",
                       "publishers", "keys", "policy"}:
            raise PackageReject("INVALID_REGISTRY", "unknown fields")
        for f in ("registryId", "version", "updatedAt", "publishers",
                  "keys", "policy"):
            if f not in doc:
                raise PackageReject("INVALID_REGISTRY", f"missing {f}")
        if not isinstance(doc["registryId"], str) or \
                not OPAQUE_ID.match(doc["registryId"]):
            raise PackageReject("INVALID_REGISTRY", "invalid registryId")
        if not isinstance(doc["version"], str) or not doc["version"] or \
                len(doc["version"]) > 128:
            raise PackageReject("INVALID_REGISTRY", "invalid version")
        _require_tz_aware(doc["updatedAt"], "updatedAt")

        publishers: dict[str, dict[str, Any]] = {}
        for p in doc["publishers"]:
            if not isinstance(p, dict) or set(p) != {
                    "publisherId", "status", "allowedKinds", "keyIds"}:
                raise PackageReject("INVALID_REGISTRY", "malformed publisher")
            if not OPAQUE_ID.match(str(p["publisherId"])):
                raise PackageReject("INVALID_REGISTRY", "invalid publisherId")
            if p["status"] not in ("active", "suspended"):
                raise PackageReject("INVALID_REGISTRY", "invalid publisher status")
            _str_list(p["allowedKinds"], "publisher.allowedKinds")
            _str_list(p["keyIds"], "publisher.keyIds")
            publishers[p["publisherId"]] = p

        keys: dict[str, dict[str, Any]] = {}
        for k in doc["keys"]:
            if not isinstance(k, dict) or set(k) != {
                    "keyId", "algorithm", "publicKey", "status",
                    "notBefore", "notAfter"}:
                raise PackageReject("INVALID_REGISTRY", "malformed key")
            if not OPAQUE_ID.match(str(k["keyId"])):
                raise PackageReject("INVALID_REGISTRY", "invalid keyId")
            if k["algorithm"] not in ALGORITHMS:
                raise PackageReject("INVALID_REGISTRY", "unknown key algorithm")
            if k["status"] not in ("active", "revoked"):
                raise PackageReject("INVALID_REGISTRY", "invalid key status")
            _require_tz_aware(k["notBefore"], "notBefore")
            if k["notAfter"] is not None:
                _require_tz_aware(k["notAfter"], "notAfter")
            keys[k["keyId"]] = k

        policy = doc["policy"]
        if not isinstance(policy, dict) or set(policy) - {
                "allowedKinds", "capabilityCatalog", "maxCapabilitiesPerKind",
                "maxPackageBytes", "rollbackRequiresApproval", "policyVersion"}:
            raise PackageReject("INVALID_REGISTRY", "policy has unknown fields")
        for f in ("allowedKinds", "capabilityCatalog", "maxPackageBytes",
                  "rollbackRequiresApproval", "policyVersion"):
            if f not in policy:
                raise PackageReject("INVALID_REGISTRY", f"policy.{f} missing")
        if not isinstance(policy["maxPackageBytes"], int) or isinstance(
                policy["maxPackageBytes"], bool) or policy["maxPackageBytes"] <= 0:
            raise PackageReject("INVALID_REGISTRY", "invalid maxPackageBytes")
        if not isinstance(policy["rollbackRequiresApproval"], bool):
            raise PackageReject("INVALID_REGISTRY", "rollbackRequiresApproval not bool")
        if not isinstance(policy["policyVersion"], str) or \
                not policy["policyVersion"] or len(policy["policyVersion"]) > 128:
            raise PackageReject("INVALID_REGISTRY", "invalid policyVersion")
        _str_list(policy["allowedKinds"], "policy.allowedKinds")
        _str_list(policy["capabilityCatalog"], "policy.capabilityCatalog")
        mcpk = policy.get("maxCapabilitiesPerKind")
        if mcpk is not None and (
                not isinstance(mcpk, dict)
                or not all(isinstance(k, str) for k in mcpk)
                or not all(isinstance(v, int) and not isinstance(v, bool)
                           for v in mcpk.values())):
            raise PackageReject("INVALID_REGISTRY", "invalid maxCapabilitiesPerKind")

        for pub_id, pub in publishers.items():
            for kid in pub["keyIds"]:
                if kid not in keys:
                    raise PackageReject(
                        "INVALID_REGISTRY",
                        f"{pub_id} references missing key {kid}")

        return cls(doc["registryId"], doc["version"], publishers, keys,
                   policy)

    def publisher(self, publisher_id: str) -> dict[str, Any] | None:
        return self.publishers.get(publisher_id)

    def key(self, key_id: str) -> dict[str, Any] | None:
        return self.keys.get(key_id)

    def key_window_ok(self, key: dict[str, Any], now: datetime) -> bool:
        nb = _require_tz_aware(key["notBefore"], "notBefore")
        na = key["notAfter"]
        na_dt = _require_tz_aware(na, "notAfter") if na is not None else None
        now_utc = now.astimezone(UTC)
        if now_utc < nb.astimezone(UTC):
            return False
        return not (na_dt is not None and now_utc > na_dt.astimezone(UTC))

    def max_caps_for(self, kind: str) -> int | None:
        per_kind = self.policy.get("maxCapabilitiesPerKind") or {}
        return per_kind.get(kind)
