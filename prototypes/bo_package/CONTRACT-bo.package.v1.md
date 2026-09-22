# Contract `bo.package.v1` — PROPUS (VAL2-00, neratificat)

Stare: **propunere de contract**, în așteptarea ratificării Codex și a
artefactului de verificare A02. Nu este funcționalitate acceptată și nu e
activată în runtime. Inspirație: manifestul witness MetaHarness
(`crates/kernel/src/witness.rs` @ `d5833dc6512ac1adeeef91a331c29055cd8a4dbb`,
MIT) — idei, nu cod preluat.

## 1. Manifestul — `manifest.json` în rădăcina pachetului

Schemă închisă (`bo.package.v1.schema.json`): câmpuri necunoscute → respingere.

| Câmp | Tip | Obligatoriu | Semnificație |
|---|---|---|---|
| `schemaVersion` | const `"bo.package.v1"` | da | |
| `packageId` | opaqueId `^[a-z0-9][a-z0-9._-]{0,63}$` | da | identitate stabilă a pachetului |
| `version` | semver `MAJOR.MINOR.PATCH` (+`-pre` opțional) | da | versiunea conținutului |
| `kind` | `bobot` \| `agent` \| `workflow` | da | ce instalează |
| `publisherId` | opaqueId | da | emitent — trebuie înrolat în registru |
| `keyId` | opaqueId | da | cheia de semnare — din registru, nu din pachet |
| `createdAt` | RFC3339, fus UTC explicit | da | |
| `artifactDigests` | obiect: `cale/relativă → "sha256:<64hex>"` | da | inventarul semnat al fișierelor |
| `dependencies` | listă `{packageId, versionRange}` | da (poate fi `[]`) | max 16; `versionRange` = `a.b.c`, `>=a.b.c`, `a.b.c-b` |
| `compatibility` | `{minHost, maxHost}` semver \| null | da | refuz dacă hostul e în afara intervalului |
| `requestedCapabilities` | listă de capabilități din catalog | da (poate fi `[]`) | orice capabilitate necunoscută → respingere |
| `settingsSchemaRef` | opaqueId + digest opțional | nu | schema de setări pe care pachetul o declară |
| `signature` | `{algorithm: "ed25519", value: base64}` | da | peste manifestul canonicalizat **fără** `signature` |

### Limite dure (validatorul le aplică înainte de cripto)

- manifest ≤ 256 KiB; `artifactDigests` ≤ 256 intrări; fiecare cale ≤ 256 car.;
- căile: relative, fără `..`, fără slash absolut, fără NUL; segmentele nu pot fi
  `.`, `..`, sau conține `\\`;
- `requestedCapabilities` ≤ 32; `dependencies` ≤ 16; stringuri ≤ 512 car.;
- numerele resping `bool`; fără câmpuri duplicate (canonicalizarea le respinge).

## 2. Canonicalizare și semnătură

- **Canonicalizare**: subset JCS (RFC 8785) — chei sortate lexicografic,
  separatori `,`/`:` fără spații, UTF-8, numerele întregi fără zecimale
  redundante, stringuri JSON-escapate minim, `signature` exclus din payload.
  Rezultat: octeți deterministici, verificabili pe orice implementare.
- **Algoritm**: Ed25519 (RFC 8032). `signature.value` = base64 standard.
- **Digest artefact**: `sha256:<hex lowercase>` peste octeții fișierului.

Cheia publică din pachet **nu** este suficientă (defectul upstream documentat
în studiu: `verify_manifest` se bazeză pe cheia din manifest). Verificarea
folosește exclusiv registrul de încredere administrat separat.

## 3. Registrul de încredere — `bo.package.registry.v1` (separat)

```json
{
  "schemaVersion": "bo.package.registry.v1",
  "registryId": "reg_x",
  "updatedAt": "<ts>",
  "publishers": [{"publisherId": "...", "status": "active|suspended",
                  "allowedKinds": ["bobot"], "keyIds": ["key_..."]}],
  "keys": [{"keyId": "...", "algorithm": "ed25519",
            "publicKey": "<base64>", "status": "active|revoked",
            "notBefore": "<ts>", "notAfter": "<ts|null>"}],
  "policy": {
    "allowedKinds": ["bobot","agent","workflow"],
    "capabilityCatalog": ["bots:simulate","bots:write","settings:read",...],
    "maxPackageBytes": 8388608,
    "rollbackRequiresApproval": true,
    "approvedRollbacks": [{"packageId":"...","toVersion":"x.y.z","approvalRef":"apr_..."}]
  }
}
```

## 4. Verificare — ordinea exactă (scurtcircuit la prima respingere)

1. `manifest.json` parse + schemă + limite → `INVALID_MANIFEST`.
2. `publisherId` înrolat și `status=active` → `PUBLISHER_UNKNOWN` /
   `PUBLISHER_SUSPENDED`.
3. `keyId` aparține publisherului, `status=active`, în fereastra
   `notBefore/notAfter` → `KEY_UNKNOWN` / `KEY_REVOKED` / `KEY_EXPIRED`.
4. Semnătură Ed25519 verifică canonicalizarea → `BAD_SIGNATURE`.
5. Fiecare cale din `artifactDigests`: există, nu e symlink, digestul real =
   declarat → `ARTIFACT_MISSING` / `ARTIFACT_MODIFIED` / `TRAVERSAL`.
   Fișiere în pachet dar nesemnate → `UNSIGNED_ARTIFACT`.
6. `compatibility` vs versiunea host → `INCOMPATIBLE`.
7. `requestedCapabilities` ⊆ `capabilityCatalog` → `CAPABILITY_UNKNOWN`;
   peste pragul per-kind → `CAPABILITY_EXCESSIVE`.
8. Versiunea vs cea instalată: `version <= instalată` fără intrare
   `approvedRollbacks` corespunzătoare → `ROLLBACK_UNAUTHORIZED`.
9. Dimensiunea totală a artefactelor ≤ `maxPackageBytes` → `PACKAGE_TOO_LARGE`.

Verdict: `ACCEPT` | `REJECT:<motiv>`. (Carantina/draft/aprobare/activare din
ciclul de viață BO-R02 urmează în VAL2-01 — prototipul dovedește doar
producerea și verificarea.)

## 5. Ce NU dovedește semnătura

Integritate și proveniență conform trust-store — **nu** corectitudine
juridică, funcțională sau de siguranță a comportamentului. Licența comercială
nu acordă drepturi tehnice suplimentare.
