"""Producerea unui pachet sintetic bo.package.v1 — prototip izolat.

Scrie artefactele + manifest.json semnat într-un director. Nu publică, nu
instalează, nu are efecte în afara directorului țintă.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bo_pkg.canon import canonical_bytes
from bo_pkg.contract import MANIFEST_NAME, SCHEMA_VERSION, validate_manifest
from bo_pkg.signing import sign


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return f"sha256:{h.hexdigest()}"


def build_manifest(
    *,
    package_id: str,
    version: str,
    kind: str,
    publisher_id: str,
    key_id: str,
    artifacts: dict[str, str],
    created_at: str | None = None,
    dependencies: list[dict[str, str]] | None = None,
    compatibility: dict[str, str | None] | None = None,
    requested_capabilities: list[str] | None = None,
    settings_schema_ref: str | None = None,
) -> dict[str, Any]:
    """Manifest nesemnat — `artifacts` mapează cale relativă → digest declarat."""
    manifest: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "packageId": package_id,
        "version": version,
        "kind": kind,
        "publisherId": publisher_id,
        "keyId": key_id,
        "createdAt": created_at or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "artifactDigests": artifacts,
        "dependencies": dependencies or [],
        "compatibility": compatibility or {"minHost": None, "maxHost": None},
        "requestedCapabilities": requested_capabilities or [],
        "signature": {"algorithm": "ed25519", "value": ""},
    }
    if settings_schema_ref is not None:
        manifest["settingsSchemaRef"] = settings_schema_ref
    return manifest


def sign_manifest(manifest: dict[str, Any], sk: Ed25519PrivateKey) -> dict[str, Any]:
    payload = canonical_bytes({k: v for k, v in manifest.items() if k != "signature"})
    out = dict(manifest)
    out["signature"] = {"algorithm": "ed25519", "value": sign(sk, payload)}
    return out


def write_package(
    dest: Path,
    files: dict[str, bytes],
    sk: Ed25519PrivateKey,
    **manifest_kwargs: Any,
) -> dict[str, Any]:
    """Scrie `files` în `dest`, apoi manifest.json semnat peste digests reale."""
    dest.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for rel, content in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        digests[rel] = f"sha256:{hashlib.sha256(content).hexdigest()}"
    manifest = build_manifest(artifacts=digests, **manifest_kwargs)
    manifest = sign_manifest(manifest, sk)
    validate_manifest(manifest)
    (dest / MANIFEST_NAME).write_bytes(
        json.dumps(manifest, indent=2, ensure_ascii=False).encode() + b"\n"
    )
    return manifest
