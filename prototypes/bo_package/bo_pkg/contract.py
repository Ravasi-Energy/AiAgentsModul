"""Validarea manifestului bo.package.v1 — schemă închisă, limite, semver.

Validare manuală (nu jsonschema) pentru a reproduce exact regulile din
CONTRACT-bo.package.v1.md și a nu adăuga dependențe prototipului.
"""

import re
from datetime import datetime
from typing import Any

from bo_pkg.errors import PackageReject

SCHEMA_VERSION = "bo.package.v1"
KINDS = {"bobot", "agent", "workflow"}
OPAQUE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[a-z0-9.-]{1,32})?$")
VERSION_RANGE = re.compile(
    r"^(>=)?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))?$"
)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
CAPABILITY = re.compile(r"^[a-z][a-z0-9:_-]{1,63}$")
B64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

MANIFEST_MAX_BYTES = 256 * 1024
MAX_ARTIFACTS = 256
MAX_DEPS = 16
MAX_CAPS = 32
MAX_STRING = 512

_FIELDS = {
    "schemaVersion", "packageId", "version", "kind", "publisherId", "keyId",
    "createdAt", "artifactDigests", "dependencies", "compatibility",
    "requestedCapabilities", "settingsSchemaRef", "signature",
}
_REQUIRED = _FIELDS - {"settingsSchemaRef"}


def parse_semver(text: str) -> tuple[int, int, int, str]:
    if not isinstance(text, str):
        raise PackageReject("INVALID_MANIFEST", f"semver nu e string: {text!r}")
    m = SEMVER.match(text)
    if not m:
        raise PackageReject("INVALID_MANIFEST", f"semver invalid: {text!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


def semver_cmp(a: tuple[int, int, int, str], b: tuple[int, int, int, str]) -> int:
    """Comparare semver: prerelease < release la aceeași tripletă."""
    ka, kb = a[:3], b[:3]
    if ka != kb:
        return -1 if ka < kb else 1
    if a[3] == b[3]:
        return 0
    if a[3] == "":
        return 1
    if b[3] == "":
        return -1
    return -1 if a[3] < b[3] else 1


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool) or not OPAQUE_ID.match(value):
        raise PackageReject("INVALID_MANIFEST", f"{field}: identificator invalid")
    return value


MANIFEST_NAME = "manifest.json"


def _check_path(path: str) -> None:
    if path == MANIFEST_NAME:
        raise PackageReject("INVALID_MANIFEST",
                            "manifest.json nu poate fi artefact semnat")
    if len(path) > 256 or path.startswith("/"):
        raise PackageReject("TRAVERSAL", f"cale interzisă: {path!r}")
    if "\x00" in path or "\\" in path:
        raise PackageReject("TRAVERSAL", f"caracter interzis în cale: {path!r}")
    for seg in path.split("/"):
        if seg in ("", ".", "..") or not PATH_SEGMENT.match(seg):
            raise PackageReject("TRAVERSAL", f"segment interzis în {path!r}")


def validate_manifest(manifest: Any) -> dict[str, Any]:
    """Validează structura manifestului. Nu verifică semnătura criptografică."""
    if not isinstance(manifest, dict):
        raise PackageReject("INVALID_MANIFEST", "manifestul nu este obiect")
    unknown = set(manifest) - _FIELDS
    if unknown:
        raise PackageReject("INVALID_MANIFEST", f"câmpuri necunoscute: {sorted(unknown)}")
    missing = _REQUIRED - set(manifest)
    if missing:
        raise PackageReject("INVALID_MANIFEST", f"câmpuri lipsă: {sorted(missing)}")

    if manifest["schemaVersion"] != SCHEMA_VERSION:
        raise PackageReject("INVALID_MANIFEST", f"schemaVersion={manifest['schemaVersion']!r}")

    _id(manifest["packageId"], "packageId")
    _id(manifest["publisherId"], "publisherId")
    _id(manifest["keyId"], "keyId")
    parse_semver(manifest["version"])

    if manifest["kind"] not in KINDS:
        raise PackageReject("INVALID_MANIFEST", f"kind invalid: {manifest['kind']!r}")

    created = manifest["createdAt"]
    try:
        dt = datetime.fromisoformat(str(created))
    except (TypeError, ValueError):
        raise PackageReject("INVALID_MANIFEST", "createdAt nu e RFC3339") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise PackageReject("INVALID_MANIFEST", "createdAt fără fus orar explicit")

    digests = manifest["artifactDigests"]
    if not isinstance(digests, dict) or not 1 <= len(digests) <= MAX_ARTIFACTS:
        raise PackageReject("INVALID_MANIFEST", "artifactDigests: dimensiune invalidă")
    for path, dg in digests.items():
        if not isinstance(path, str):
            raise PackageReject("INVALID_MANIFEST", "cale non-string")
        _check_path(path)
        if not isinstance(dg, str) or not DIGEST.match(dg):
            raise PackageReject("INVALID_MANIFEST", f"digest invalid pentru {path!r}")

    deps = manifest["dependencies"]
    if not isinstance(deps, list) or len(deps) > MAX_DEPS:
        raise PackageReject("INVALID_MANIFEST", "dependencies: dimensiune invalidă")
    for dep in deps:
        if not isinstance(dep, dict) or set(dep) != {"packageId", "versionRange"}:
            raise PackageReject("INVALID_MANIFEST", "dependență malformată")
        _id(dep["packageId"], "dependencies.packageId")
        if not VERSION_RANGE.match(str(dep["versionRange"])):
            raise PackageReject("INVALID_MANIFEST", f"versionRange invalid: {dep['versionRange']!r}")

    compat = manifest["compatibility"]
    if not isinstance(compat, dict) or set(compat) != {"minHost", "maxHost"}:
        raise PackageReject("INVALID_MANIFEST", "compatibility malformat")
    for bound in ("minHost", "maxHost"):
        v = compat[bound]
        if v is not None:
            parse_semver(str(v))

    caps = manifest["requestedCapabilities"]
    if not isinstance(caps, list) or len(caps) > MAX_CAPS:
        raise PackageReject("INVALID_MANIFEST", "requestedCapabilities: dimensiune invalidă")
    if len(set(caps)) != len(caps):
        raise PackageReject("INVALID_MANIFEST", "capabilități duplicate")
    for cap in caps:
        if not isinstance(cap, str) or not CAPABILITY.match(cap):
            raise PackageReject("INVALID_MANIFEST", f"capabilitate malformată: {cap!r}")

    ref = manifest.get("settingsSchemaRef")
    if ref is not None and (not isinstance(ref, str) or len(ref) > 256):
        raise PackageReject("INVALID_MANIFEST", "settingsSchemaRef invalid")

    sig = manifest["signature"]
    if not isinstance(sig, dict) or set(sig) != {"algorithm", "value"}:
        raise PackageReject("INVALID_MANIFEST", "signature malformat")
    if sig["algorithm"] != "ed25519":
        raise PackageReject("INVALID_MANIFEST", f"algoritm nesuportat: {sig['algorithm']!r}")
    if not isinstance(sig["value"], str) or len(sig["value"]) > 128 or not B64.match(sig["value"]):
        raise PackageReject("INVALID_MANIFEST", "signature.value invalid")

    for field in ("packageId", "publisherId", "keyId", "version"):
        if len(manifest[field]) > MAX_STRING:
            raise PackageReject("INVALID_MANIFEST", f"{field} prea lung")

    return manifest
