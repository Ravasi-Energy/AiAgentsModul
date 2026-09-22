"""Registru de încredere bo.package.registry.v1 — magazin administrat separat.

Pachetul NU poartă autoritatea: cheia publică vine exclusiv din acest registru.
În prototip registru = document JSON validat; în produs ar fi un store
administrat cu audit și CAS (ca bo_settings), iar revocarea = mutație auditată.
"""

from datetime import datetime, timezone
from typing import Any

from bo_pkg.contract import OPAQUE_ID, parse_semver, semver_cmp
from bo_pkg.errors import PackageReject

REGISTRY_SCHEMA_VERSION = "bo.package.registry.v1"
ALGORITHMS = {"ed25519"}


class TrustRegistry:
    def __init__(
        self,
        registry_id: str,
        publishers: dict[str, dict[str, Any]],
        keys: dict[str, dict[str, Any]],
        policy: dict[str, Any],
    ) -> None:
        self.registry_id = registry_id
        self.publishers = publishers
        self.keys = keys
        self.policy = policy

    @classmethod
    def from_dict(cls, doc: Any) -> "TrustRegistry":
        if not isinstance(doc, dict) or doc.get("schemaVersion") != REGISTRY_SCHEMA_VERSION:
            raise PackageReject("INVALID_REGISTRY", "schemaVersion greșit")
        if set(doc) - {"schemaVersion", "registryId", "updatedAt", "publishers", "keys", "policy"}:
            raise PackageReject("INVALID_REGISTRY", "câmpuri necunoscute")
        for f in ("registryId", "updatedAt", "publishers", "keys", "policy"):
            if f not in doc:
                raise PackageReject("INVALID_REGISTRY", f"lipsește {f}")
        if not isinstance(doc["registryId"], str) or not OPAQUE_ID.match(doc["registryId"]):
            raise PackageReject("INVALID_REGISTRY", "registryId invalid")

        publishers: dict[str, dict[str, Any]] = {}
        for p in doc["publishers"]:
            if not isinstance(p, dict) or set(p) - {"publisherId", "status", "allowedKinds", "keyIds"}:
                raise PackageReject("INVALID_REGISTRY", "publisher malformat")
            if set(p) != {"publisherId", "status", "allowedKinds", "keyIds"}:
                raise PackageReject("INVALID_REGISTRY", "publisher incomplet")
            if not OPAQUE_ID.match(str(p["publisherId"])):
                raise PackageReject("INVALID_REGISTRY", "publisherId invalid")
            if p["status"] not in ("active", "suspended"):
                raise PackageReject("INVALID_REGISTRY", "status publisher invalid")
            if not isinstance(p["allowedKinds"], list) or not isinstance(p["keyIds"], list):
                raise PackageReject("INVALID_REGISTRY", "publisher malformat")
            publishers[p["publisherId"]] = p

        keys: dict[str, dict[str, Any]] = {}
        for k in doc["keys"]:
            if not isinstance(k, dict) or set(k) != {
                "keyId", "algorithm", "publicKey", "status", "notBefore", "notAfter"
            }:
                raise PackageReject("INVALID_REGISTRY", "cheie malformată")
            if not OPAQUE_ID.match(str(k["keyId"])):
                raise PackageReject("INVALID_REGISTRY", "keyId invalid")
            if k["algorithm"] not in ALGORITHMS:
                raise PackageReject("INVALID_REGISTRY", "algoritm cheie necunoscut")
            if k["status"] not in ("active", "revoked"):
                raise PackageReject("INVALID_REGISTRY", "status cheie invalid")
            for field in ("notBefore", "notAfter"):
                ts = k[field]
                if ts is None:
                    if field == "notBefore":
                        raise PackageReject("INVALID_REGISTRY", "notBefore obligatoriu")
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts))
                except (TypeError, ValueError):
                    raise PackageReject("INVALID_REGISTRY",
                                        f"{field} nu e RFC3339") from None
                if dt.tzinfo is None or dt.utcoffset() is None:
                    raise PackageReject("INVALID_REGISTRY",
                                        f"{field} fără fus orar explicit")
            keys[k["keyId"]] = k

        policy = doc["policy"]
        if not isinstance(policy, dict) or set(policy) - {
            "allowedKinds", "capabilityCatalog", "maxCapabilitiesPerKind",
            "maxPackageBytes", "rollbackRequiresApproval", "approvedRollbacks",
        }:
            raise PackageReject("INVALID_REGISTRY", "politica are câmpuri necunoscute")
        for f in ("allowedKinds", "capabilityCatalog", "maxPackageBytes",
                  "rollbackRequiresApproval", "approvedRollbacks"):
            if f not in policy:
                raise PackageReject("INVALID_REGISTRY", f"policy.{f} lipsă")
        if not isinstance(policy["maxPackageBytes"], int) or isinstance(
            policy["maxPackageBytes"], bool
        ) or policy["maxPackageBytes"] <= 0:
            raise PackageReject("INVALID_REGISTRY", "maxPackageBytes invalid")
        for rb in policy["approvedRollbacks"]:
            if set(rb) != {"packageId", "toVersion", "approvalRef"}:
                raise PackageReject("INVALID_REGISTRY", "approvedRollbacks malformat")
            parse_semver(str(rb["toVersion"]))

        for pub_id, pub in publishers.items():
            for kid in pub["keyIds"]:
                if kid not in keys:
                    raise PackageReject("INVALID_REGISTRY",
                                        f"{pub_id} referențiază cheia inexistentă {kid}")

        return cls(doc["registryId"], publishers, keys, policy)

    def publisher(self, publisher_id: str) -> dict[str, Any] | None:
        return self.publishers.get(publisher_id)

    def key(self, key_id: str) -> dict[str, Any] | None:
        return self.keys.get(key_id)

    def key_window_ok(self, key: dict[str, Any], now: datetime) -> bool:
        nb = datetime.fromisoformat(key["notBefore"])
        na = key["notAfter"]
        na_dt = datetime.fromisoformat(na) if na else None
        now_utc = now.astimezone(timezone.utc)
        if now_utc < nb.astimezone(timezone.utc):
            return False
        return not (na_dt is not None and now_utc > na_dt.astimezone(timezone.utc))

    def rollback_approved(self, package_id: str, to_version: str) -> bool:
        target = parse_semver(to_version)
        for rb in self.policy["approvedRollbacks"]:
            if rb["packageId"] == package_id and \
                    semver_cmp(parse_semver(rb["toVersion"]), target) == 0:
                return True
        return False

    def max_caps_for(self, kind: str) -> int | None:
        per_kind = self.policy.get("maxCapabilitiesPerKind") or {}
        return per_kind.get(kind)
