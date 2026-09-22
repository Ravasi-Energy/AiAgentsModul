#!/usr/bin/env python3
"""Regenerează fixture-urile prototipului bo.package.v1 (determinist).

Produce `fixtures/keys.json` (chei SINTETICE Ed25519), `fixtures/registry.json`
(bo.package.registry.v1) și `fixtures/packages/<caz>/` — un director de pachet
per scenariu, cu `expected.json` (verdictul așteptat). Idempotent: rescrie tot.

Rulează din rădăcina repo-ului:
    PYTHONPATH=prototypes/bo_package python prototypes/bo_package/make_fixture_packages.py
"""

import base64
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bo_pkg.build import sign_manifest, build_manifest  # noqa: E402
from bo_pkg.canon import canonical_bytes  # noqa: E402
from bo_pkg.signing import generate_keypair, private_to_b64, sign  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

# Timestamp fix → fixture-uri deterministe la nivel de manifest (octeții de
# semnătură rămân deterministici pentru că Ed25519 e deterministic).
CREATED = "2026-09-23T00:00:00Z"


def write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(doc, indent=2, ensure_ascii=False).encode() + b"\n")


EXPECTED: dict[str, dict] = {}


def write_package(
    case: str,
    files: dict[str, bytes],
    manifest: dict,
    expected: dict,
) -> None:
    dest = FIXTURES / "packages" / case
    dest.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    write_json(dest / "manifest.json", manifest)
    EXPECTED[case] = expected


def main() -> None:
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    FIXTURES.mkdir(parents=True)

    # --- chei sintetice ---
    sk_active, pk_active = generate_keypair()
    sk_revoked, pk_revoked = generate_keypair()
    sk_expired, pk_expired = generate_keypair()
    sk_stranger, pk_stranger = generate_keypair()
    keys_doc = {
        "_nota": "CHEI SINTETICE — doar pentru fixture-uri de prototip, "
                 "nu au nicio autoritate reală.",
        "key-active": private_to_b64(sk_active),
        "key-revoked": private_to_b64(sk_revoked),
        "key-expired": private_to_b64(sk_expired),
        "key-stranger": private_to_b64(sk_stranger),
    }
    write_json(FIXTURES / "keys.json", keys_doc)

    # --- registru de încredere ---
    registry = {
        "schemaVersion": "bo.package.registry.v1",
        "registryId": "reg-fixture",
        "updatedAt": CREATED,
        "publishers": [
            {"publisherId": "pub-bo", "status": "active",
             "allowedKinds": ["bobot", "agent"],
             "keyIds": ["key-active", "key-revoked", "key-expired"]},
            {"publisherId": "pub-suspended", "status": "suspended",
             "allowedKinds": ["bobot"], "keyIds": []},
        ],
        "keys": [
            {"keyId": "key-active", "algorithm": "ed25519",
             "publicKey": base64.b64encode(pk_active).decode(),
             "status": "active", "notBefore": "2026-01-01T00:00:00Z",
             "notAfter": None},
            {"keyId": "key-revoked", "algorithm": "ed25519",
             "publicKey": base64.b64encode(pk_revoked).decode(),
             "status": "revoked", "notBefore": "2026-01-01T00:00:00Z",
             "notAfter": None},
            {"keyId": "key-expired", "algorithm": "ed25519",
             "publicKey": base64.b64encode(pk_expired).decode(),
             "status": "active", "notBefore": "2026-01-01T00:00:00Z",
             "notAfter": "2026-06-01T00:00:00Z"},
        ],
        "policy": {
            "allowedKinds": ["bobot", "agent", "workflow"],
            "capabilityCatalog": [
                "bots:simulate", "bots:write", "settings:read",
                "notify:internal", "files:read",
            ],
            "maxCapabilitiesPerKind": {"bobot": 3, "agent": 8, "workflow": 8},
            "maxPackageBytes": 1048576,
            "rollbackRequiresApproval": True,
            "approvedRollbacks": [
                {"packageId": "pkg.demo-bot", "toVersion": "1.0.0",
                 "approvalRef": "apr-fixture-001"},
            ],
        },
    }
    write_json(FIXTURES / "registry.json", registry)

    BOT = json.dumps({
        "schemaVersion": "bo.bobot.v1",
        "name": "demo-bot",
        "kind": "BOT",
        "content": {"steps": [{"id": "s1", "type": "check",
                               "check": {"fact": "system.alive"}}]},
    }, indent=2).encode() + b"\n"
    DOC = b"# pachet de demo\n\nContinut sintetic pentru prototipul VAL2-00.\n"

    def signable(**kw):
        base = dict(
            package_id="pkg.demo-bot", version="1.3.0", kind="bobot",
            publisher_id="pub-bo", key_id="key-active", created_at=CREATED,
        )
        base.update(kw)
        return base

    def signed(files, sk, **kw):
        digests = {rel: f"sha256:{hashlib.sha256(c).hexdigest()}"
                   for rel, c in files.items()}
        return sign_manifest(build_manifest(artifacts=digests, **kw), sk)

    files = {"bots/demo.bobot.json": BOT, "docs/README.md": DOC}

    # pozitiv
    write_package("valid-bobot", files, signed(files, sk_active, **signable()),
                  {"verdict": "ACCEPT"})

    # semnătură coruptă (manifest valid, semnătura nu verifică)
    m = signed(files, sk_active, **signable())
    sig = base64.b64decode(m["signature"]["value"])
    m["signature"]["value"] = base64.b64encode(bytes([sig[0] ^ 1]) + sig[1:]).decode()
    write_package("bad-signature", files, m, {"verdict": "REJECT", "code": "BAD_SIGNATURE"})

    # artefact modificat după semnare
    tampered = dict(files)
    tampered["bots/demo.bobot.json"] = BOT.replace(b'"system.alive"', b'"system.gone"')
    write_package("tampered-artifact", tampered,
                  signed(files, sk_active, **signable()),  # semnat peste digests vechi
                  {"verdict": "REJECT", "code": "ARTIFACT_MODIFIED"})

    # artefact lipsă
    missing = dict(files)
    del missing["docs/README.md"]
    write_package("missing-artifact", missing,
                  signed(files, sk_active, **signable()),
                  {"verdict": "REJECT", "code": "ARTIFACT_MISSING"})

    # fișier nesemnat în plus
    extra = dict(files)
    extra["smuggled.py"] = b"import os\n"
    write_package("unsigned-artifact", extra,
                  signed(files, sk_active, **signable()),
                  {"verdict": "REJECT", "code": "UNSIGNED_ARTIFACT"})

    # emitent necunoscut (semnat corect, dar cu cheie din afara registrului)
    m = signed(files, sk_stranger, **signable(publisher_id="pub-stranger",
                                            key_id="key-stranger"))
    write_package("unknown-signer", files, m,
                  {"verdict": "REJECT", "code": "PUBLISHER_UNKNOWN"})

    # publisher suspendat — refuzat înainte de orice verificare de cheie
    m = signed(files, sk_active, **signable(publisher_id="pub-suspended"))
    write_package("suspended-publisher", files, m,
                  {"verdict": "REJECT", "code": "PUBLISHER_SUSPENDED"})

    # cheie revocată / expirată — semnături valide, dar registrul refuză
    write_package("revoked-key", files,
                  signed(files, sk_revoked, **signable(key_id="key-revoked")),
                  {"verdict": "REJECT", "code": "KEY_REVOKED"})
    write_package("expired-key", files,
                  signed(files, sk_expired, **signable(key_id="key-expired")),
                  {"verdict": "REJECT", "code": "KEY_EXPIRED"})

    # host incompatibil
    write_package("incompatible-host", files,
                  signed(files, sk_active,
                         **signable(compatibility={"minHost": "99.0.0",
                                                   "maxHost": None})),
                  {"verdict": "REJECT", "code": "INCOMPATIBLE"})

    # capabilități
    write_package("unknown-capability", files,
                  signed(files, sk_active,
                         **signable(requested_capabilities=["net:egress"])),
                  {"verdict": "REJECT", "code": "CAPABILITY_UNKNOWN"})
    write_package("excessive-capabilities", files,
                  signed(files, sk_active, **signable(requested_capabilities=[
                      "bots:simulate", "bots:write", "settings:read",
                      "notify:internal", "files:read"])),
                  {"verdict": "REJECT", "code": "CAPABILITY_EXCESSIVE"})

    # rollback: versiune 1.0.0 peste instalat 1.2.0 — neaprobat vs aprobat
    write_package("rollback-unauthorized", files,
                  signed(files, sk_active, **signable(version="1.0.0",
                                                      package_id="pkg.other-bot")),
                  {"verdict": "REJECT", "code": "ROLLBACK_UNAUTHORIZED"})
    write_package("rollback-approved", files,
                  signed(files, sk_active, **signable(version="1.0.0")),
                  {"verdict": "ACCEPT"})

    # traversal în manifest (semnat corect — refuzat la etapa de schemă)
    evil_digests = {rel: f"sha256:{hashlib.sha256(c).hexdigest()}"
                    for rel, c in files.items()}
    evil_digests["../escape.txt"] = "sha256:" + "0" * 64
    m = sign_manifest(build_manifest(artifacts=evil_digests, **signable()), sk_active)
    write_package("traversal", files, m, {"verdict": "REJECT", "code": "TRAVERSAL"})

    # manifest malformat
    dest = FIXTURES / "packages" / "malformed-manifest"
    dest.mkdir(parents=True)
    for rel, c in files.items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(c)
    (dest / "manifest.json").write_bytes(b'{"schemaVersion": "bo.package.v1", ')
    EXPECTED["malformed-manifest"] = {"verdict": "REJECT", "code": "INVALID_MANIFEST"}

    # câmp necunoscut în manifest (resemnat corect — refuzat la schemă)
    m = signed(files, sk_active, **signable())
    m["tenantNote"] = "câmp arbitrar"
    m["signature"]["value"] = sign(
        sk_active,
        canonical_bytes({k: v for k, v in m.items() if k != "signature"}),
    )
    write_package("extra-field", files, m,
                  {"verdict": "REJECT", "code": "INVALID_MANIFEST"})

    write_json(FIXTURES / "expected.json",
               {"_nota": "verdictul așteptat per caz — separat de pachete, "
                         "ca să nu conteze drept artefact nesemnat",
                "cases": EXPECTED})
    print(f"fixture-uri generate în {FIXTURES}")


if __name__ == "__main__":
    main()
