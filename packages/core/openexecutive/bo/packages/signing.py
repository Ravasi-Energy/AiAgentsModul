"""Ed25519 helpers for bo.package.v1 — `cryptography` is already a dependency."""
from __future__ import annotations

import base64
import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from openexecutive.bo.packages.errors import PackageReject


def private_from_b64(text: str) -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(text))
    except (ValueError, binascii.Error) as e:
        raise PackageReject("INVALID_REGISTRY", "invalid private key fixture") from e


def public_from_b64(text: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(text))
    except (ValueError, binascii.Error) as e:
        raise PackageReject("INVALID_REGISTRY", "invalid public key in registry") from e


def sign(sk: Ed25519PrivateKey, payload: bytes) -> str:
    return base64.b64encode(sk.sign(payload)).decode()


def verify(pk: Ed25519PublicKey, payload: bytes, signature_b64: str) -> bool:
    try:
        pk.verify(base64.b64decode(signature_b64), payload)
        return True
    except (InvalidSignature, binascii.Error, ValueError):
        return False
