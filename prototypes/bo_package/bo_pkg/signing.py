"""Semnare/verificare Ed25519 pentru prototipul bo.package.v1.

Cheile fixturelor sunt SINTETICE, generate local pentru prototip — nu sunt
chei de produs. Biblioteca: `cryptography` (deja dependență a repo-ului).
"""

import base64
import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from bo_pkg.errors import PackageReject


def generate_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key().public_bytes_raw()


def private_to_b64(sk: Ed25519PrivateKey) -> str:
    return base64.b64encode(sk.private_bytes_raw()).decode()


def private_from_b64(text: str) -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(text))
    except (ValueError, binascii.Error) as e:
        raise PackageReject("INVALID_REGISTRY", "cheie privată fixture invalidă") from e


def public_from_b64(text: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(text))
    except (ValueError, binascii.Error) as e:
        raise PackageReject("INVALID_REGISTRY", "cheie publică invalidă în registru") from e


def sign(sk: Ed25519PrivateKey, payload: bytes) -> str:
    return base64.b64encode(sk.sign(payload)).decode()


def verify(pk: Ed25519PublicKey, payload: bytes, signature_b64: str) -> bool:
    try:
        pk.verify(base64.b64decode(signature_b64), payload)
        return True
    except (InvalidSignature, binascii.Error, ValueError):
        return False
