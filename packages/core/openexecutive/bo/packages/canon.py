"""BO-C14N-v1 — canonical serialization for the bo.package.v1 signed payload.

Own subset (per the coordinator decision this is NOT claimed as RFC8785/JCS
compliance): objects sorted by key, `,`/`:` separators with no whitespace,
UTF-8 output, minimal JSON string escaping, integers only (floats rejected),
duplicate keys rejected at parse time (see `contract.load_manifest`).

`surface_escape` decisions: ``ensure_ascii=False`` keeps non-ASCII content as
UTF-8 bytes; control characters, `"` and `\\` are escaped by json.dumps.
"""
from __future__ import annotations

import json
from typing import Any

from openexecutive.bo.packages.errors import PackageReject

ALGORITHM_ID = "BO-C14N-v1"


def _canon_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _canon(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _canon_str(value)
    if isinstance(value, bool):
        raise PackageReject("INVALID_MANIFEST", "bool in numeric position")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise PackageReject("INVALID_MANIFEST", "floats are not permitted in a manifest")
    if isinstance(value, list):
        return "[" + ",".join(_canon(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value.keys()):
            if not isinstance(key, str):
                raise PackageReject("INVALID_MANIFEST", "non-string key")
            parts.append(f"{_canon_str(key)}:{_canon(value[key])}")
        return "{" + ",".join(parts) + "}"
    raise PackageReject("INVALID_MANIFEST", f"unsupported type: {type(value).__name__}")


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic bytes of a JSON object — the Ed25519 input."""
    if not isinstance(payload, dict):
        raise PackageReject("INVALID_MANIFEST", "signed payload must be an object")
    return _canon(payload).encode("utf-8")


def signed_payload(manifest: dict[str, Any]) -> bytes:
    """Canonical bytes of the manifest with the whole `signature` field omitted."""
    if "signature" not in manifest:
        raise PackageReject("INVALID_MANIFEST", "missing signature")
    return canonical_bytes({k: v for k, v in manifest.items() if k != "signature"})
