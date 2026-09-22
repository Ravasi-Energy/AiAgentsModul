"""Canonicalizare subset-JCS (RFC 8785) pentru payload-ul semnat bo.package.v1.

Reguli:
- obiecte: chei sortate lexicografic UTF-8, fără duplicate, `:` și `,` fără spații;
- stringuri: escapare JSON minimă (control chars, " și \\); ensure_ascii=False → UTF-8;
- numere: int → cifre; float → respinse (contractul nu are numere zecimale);
- bool/null → literali JSON;
- `None` la nivel de cheie de obiect este respins (câmpurile opționale se omit).
"""

import json
from typing import Any

from bo_pkg.errors import PackageReject


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
        raise PackageReject("INVALID_MANIFEST", "bool în poziție numerică")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise PackageReject("INVALID_MANIFEST", "numere zecimale nepermise în manifest")
    if isinstance(value, list):
        return "[" + ",".join(_canon(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value.keys()):
            if not isinstance(key, str):
                raise PackageReject("INVALID_MANIFEST", "cheie non-string")
            parts.append(f"{_canon_str(key)}:{_canon(value[key])}")
        return "{" + ",".join(parts) + "}"
    raise PackageReject("INVALID_MANIFEST", f"tip nesuportat: {type(value).__name__}")


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Octeții canonici ai unui obiect JSON — intrarea în Ed25519."""
    if not isinstance(payload, dict):
        raise PackageReject("INVALID_MANIFEST", "payload-ul semnat trebuie să fie obiect")
    return _canon(payload).encode("utf-8")


def signed_payload(manifest: dict[str, Any]) -> bytes:
    """Manifestul canonicalizat fără câmpul `signature` (proprietatea se omit,
    nu se nulează — un manifest cu `signature: null` e invalid oricum)."""
    if "signature" not in manifest:
        raise PackageReject("INVALID_MANIFEST", "lipsește signature")
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return canonical_bytes(body)
