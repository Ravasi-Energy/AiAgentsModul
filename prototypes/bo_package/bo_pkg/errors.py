"""Coduri de respingere ale prototipului bo.package.v1.

Fiecare respingere are un cod stabil — contractul (CONTRACT-bo.package.v1.md)
enumeră ordinea exactă; acestea sunt și cazurile de test negative.
"""


class PackageReject(Exception):
    """Respingere deterministă a unui pachet sau registru."""

    CODES = {
        "INVALID_MANIFEST",
        "PUBLISHER_UNKNOWN",
        "PUBLISHER_SUSPENDED",
        "KEY_UNKNOWN",
        "KEY_REVOKED",
        "KEY_EXPIRED",
        "BAD_SIGNATURE",
        "ARTIFACT_MISSING",
        "ARTIFACT_MODIFIED",
        "UNSIGNED_ARTIFACT",
        "TRAVERSAL",
        "INCOMPATIBLE",
        "CAPABILITY_UNKNOWN",
        "CAPABILITY_EXCESSIVE",
        "ROLLBACK_UNAUTHORIZED",
        "PACKAGE_TOO_LARGE",
        "INVALID_REGISTRY",
    }

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in self.CODES:
            raise ValueError(f"cod de respingere necunoscut: {code}")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)
