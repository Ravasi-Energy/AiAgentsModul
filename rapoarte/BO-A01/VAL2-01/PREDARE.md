# PREDARE — BO-A01 / VAL2-01 (remediere după review)

> **Referința curentă de livrare este
> `coordonare/rapoarte/BO-A01/VAL2-01/PREDARE.md`** (în afara repo-ului,
> actualizată la head-ul final). Acest fișier este istoricul pe repo.

Stare: **TESTAT_LOCAL** (nu acceptat — verificarea independentă aparține
Codex).

| | |
|---|---|
| Repository | `Ravasi-Energy/BOAgents` |
| Branch | `bo/val2-01-a01-packages` (continuat — același PR) |
| baseSHA | `46424b5c865bde7e74d3c9f56ea148fc0ae8a4c0` |
| headSHA | `67a4387f04c172c91b12cab8d90c9f3d5de1e94c` + acest raport (commit documentar; `git rev-parse` dă vârful exact) |
| PR | https://github.com/Ravasi-Energy/BOAgents/pull/5 (draft, bază `main`) |
| Artefact comun | `coordonare/contracte/bo.package.v1/` + `MANIFEST.json` (amprentă) |

## Remediere după review (INT-201)

Convergență completă cu verificatorul independent A02 pe contractul comun:

- **`artifactSetDigest` pe formula §2** — sha256 peste JSON-ul compact al
  listei sortate de perechi `[cale, digest]` (implementarea A01 folosea
  canonicalizarea obiectului — divergență față de propriul contract și față
  de A02/Hire; corectată).
- **`manifestDigest` explicit** — sha256 peste payloadul canonic semnat
  (BO-C14N-v1 fără `signature`); identic cu octeții legați de semnătură,
  stabil la reformatare. Ambiguitatea brut-vs-canonic din review este
  rezolvată în contract §5.
- **Verdict `bo.package.verdict.v1`** — `schemaVersion` obligatoriu,
  `reasons = ["<CODE>: <detaliu>"]`, `idempotent` mereu prezent,
  `approvalRef` numai la ACCEPT autorizat, fără `signature` decorativă.
  Schema dedicată: `bo.package.verdict.v1.schema.json`.
- **`trustVersion`/`policyVersion`** = etichetele declarate din registru
  (`version`, `policy.policyVersion` — ambele obligatorii).
- **Downgrade uniform `ROLLBACK_UNAUTHORIZED`** pentru orice legătură
  nesatisfăcută; aprobările din context cer TOATE legăturile
  (tenant/package/from/to/digest/expirare) — intrări malformate →
  `REGISTRY_UNAVAILABLE` fail-closed, nu ignorate.
- `policy.approvedRollbacks` eliminat — aprobările nu sunt în trust store
  (contract §3); vin exclusiv din contextul per-tenant (DB în BOAgents).
- Fixture-uri extinse la **29** (+ `idempotent-reimport`,
  `kind-not-allowed`, `approval-expired`, `approval-foreign-tenant`,
  `approval-consumed`); `expected.json` poartă `verdict_doc` complet de
  referință.

## Probă de interoperabilitate (dovada cerută)

`interop/check_verifier.py` rulează AMBELE implementări peste toate
fixture-urile cu contextul din `expected.json`:

```
29 cazuri × 2 implementări — 0 divergențe
```

Comparate: clasificarea, `manifestDigest`, `artifactSetDigest`,
`idempotent`, `trustVersion`, `policyVersion`, `tenantRef`, `approvalRef`.

## Cerințe acoperite (primul lot)

- Import → verificare → carantină → draft în `openexecutive/bo/packages/`,
  **fără activare**, fără efecte externe.
- Limite înainte/în timpul citirii, chei duplicate, NaN/Infinity,
  `manifest.json` imbricat = artefact ordinar, symlink oriunde → TRAVERSAL.
- Idempotent pe `manifest_digest`+`artifactSetDigest`; conflict la manifest
  sau digest diferit; downgrade legat+expirant+consum unic.
- Trust store separat; 4 setări tenant (enabled off implicit); API 6
  endpoint-uri; UI `/bo/packages` românesc.

## Teste și rezultate reale

| Verificare | Rezultat |
|---|---|
| `pytest tests/unit` complet | **3639 passed, 1 skipped** |
| `test_bo_packages.py` | **66 passed** (contract + serviciu + rute) |
| ruff (fișiere atinse) | curat |
| mypy (fișiere atinse) | curat (10 fișiere) |
| `next build` | verde |
| `regen.py` artefact | 29 fixture, 26 vectori, clasificare confirmată |
| `interop/check_verifier.py` | A01×A02, 0 divergențe |
| `SHA256SUMS` | verificat OK pe tot artefactul |
| Probe UI | 11 probe (lot inițial), 0 overflow — verdictul rămâne string-rendered, forma nouă e compatibilă |

## Defecte remediate (cumulativ)

1. Trust store JSON indentat respins → validatorul acceptă `\n`/`\t`.
2. Rând REJECTED ocupa slotul UNIQUE → index parțial + cursă tratată.
3. Idempotența pe `artifact_set_digest` → acum pe `manifest_digest`.
4. (review) `artifactSetDigest`/`manifestDigest`/verdict/aprobări — vezi §remediere.

## Migrare / rollback

- Tabele `bo_package_imports`/`bo_package_approvals` în `bo_agents.db` (DDL
  idempotent). Revenire = revert + ștergere `bo_packages/`; fără efecte
  externe. `bo.packages.enabled=false` dezactivează complet importul.

## Limite

- Import dintr-un director local al serverului; fără upload de fișiere.
- Verdictul nu e aprobare de execuție; activarea nu există în acest lot.
- Cheile din `keys.json` sunt sintetice; `signature` nu poartă verdictul —
  autenticitatea vine din canalul de serviciu autentificat.
- `reasons[0]` poartă `"CODE: detaliu"` — consumatorii compară codul, nu
  detaliul.

## INT-201-06 — înghețarea artefactului comun

Versiunea curentă a `coordonare/contracte/bo.package.v1/` este **înghețată**
ca referință pentru consumatori (A02/A03). Nicio schimbare de contract
necoordonată.

**Amprentă (SHA256 peste `SHA256SUMS`):**
`02571944523f2e41f31f61829fd59bd771f053500b82ba9df8048dddacf92d99`
— identică cu cea verificată independent în review-ul de remedieri.

**Inventar:**

| Componentă | Conținut |
|---|---|
| `CONTRACT-bo.package.v1.md` | contractul complet (manifest, verificare, verdict, aprobări, coduri) |
| `bo.package.v1.schema.json` | schemă manifest — SHA256 `f070e838…95e13` (identică în toate cele 3 produse) |
| `bo.package.verdict.v1.schema.json` | schemă verdict `bo.package.verdict.v1` (mulțime închisă) |
| `BO-C14N-v1.md` | specificația canonicalizării |
| `registry.example.json` + `keys.json` | registru exemplu + chei Ed25519 sintetice |
| `fixtures/packages/` | **29 cazuri** — 4 ACCEPT (`valid-bobot`, `nested-manifest-signed`, `rollback-approved`, `idempotent-reimport`) + 25 REJECT |
| `fixtures/expected.json` | verdict complet de referință (`verdict_doc`) + context per caz |
| `vectors/` | **26 vectori canonici** `.canonical.bin` + `digests.json` |
| `regen.py` | regenerare deterministă din runtime A01 + auto-verificare clasificare |
| `interop/check_verifier.py` | probă cross-implementare (A01×A02: 0 divergențe) |
| `SHA256SUMS`, `MANIFEST.json`, `CHANGELOG.md`, `README.md` | integritate, amprentă, istoric, consum |

Coduri de respingere acoperite: `ARTIFACT_MISSING`, `ARTIFACT_MODIFIED`,
`BAD_SIGNATURE`, `CAPABILITY_EXCESSIVE`, `CAPABILITY_UNKNOWN`,
`DUPLICATE_KEY`, `INCOMPATIBLE`, `INVALID_MANIFEST`, `KEY_EXPIRED`,
`KEY_REVOKED`, `KEY_UNKNOWN`, `KIND_NOT_ALLOWED`, `MANIFEST_TOO_LARGE`,
`PUBLISHER_SUSPENDED`, `PUBLISHER_UNKNOWN`, `ROLLBACK_UNAUTHORIZED`,
`TRAVERSAL`, `UNSIGNED_ARTIFACT`, `VERSION_CONFLICT`.

Notă: copiile artefactului din Guardian/Hire aveau 24 cazuri la review;
consumul versiunii înghețate (29) aparține A02/A03 — eu nu modific
checkout-urile lor.

## FIN-01 — completări de închidere

- **`cryptography>=43.0.0` în `dependencies`** — `signing.py` o importa
  direct, dar manifestul o acoperea doar tranzitiv via `google-auth`
  (aceeași clasă ca INT-201-07). Wheel-ul construit declară
  `Requires-Dist: cryptography>=43.0.0` și conține `bo/packages/*`.
- **Matricea parametrilor** — `MATRICE-PARAMETRI.md`: 9 chei `bo.*` +
  inputurile `/bo/packages`; tip/default/limite, scope, RBAC, persistență,
  audit, aplicare, probă per parametru. Toți administrabili.
- **Re-probe pe head** — capturile `/bo/packages` + `settings-bo-pachete`
  refăcute pe verdictul `bo.package.verdict.v1` (edge/tastatură rămân
  valabile). Flux live: `enabled` oprit→pornit comută `PACKAGES_DISABLED`→
  ACCEPT; promovare DRAFT; reimport identic → `idempotent:true`.

## Artefacte

- Bundle: `agenti/BO-A01/bo-a01-val2-01.bundle` _(SHA256 în MANIFEST-VAL2-01.json)_
- Amprenta artefactului: `MANIFEST.json` → `fingerprint_sha256` =
  `02571944523f2e41f31f61829fd59bd771f053500b82ba9df8048dddacf92d99`
- Probe: `rapoarte/BO-A01/VAL2-01/probe-ui/`
- Contract comun: `coordonare/contracte/bo.package.v1/`
- Predare canonică: `coordonare/rapoarte/BO-A01/VAL2-01/PREDARE.md`
