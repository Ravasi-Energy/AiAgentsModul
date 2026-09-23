# PREDARE — BO-A01 / VAL2-01 (remediere după review)

Stare: **TESTAT_LOCAL** (nu acceptat — verificarea independentă aparține
Codex).

| | |
|---|---|
| Repository | `Ravasi-Energy/BOAgents` |
| Branch | `bo/val2-01-a01-packages` (continuat — același PR) |
| baseSHA | `46424b5c865bde7e74d3c9f56ea148fc0ae8a4c0` |
| headSHA | vârf la predare: `git rev-parse bo/val2-01-a01-packages` |
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

## Artefacte

- Bundle: `agenti/BO-A01/bo-a01-val2-01.bundle` _(SHA256 în MANIFEST-VAL2-01.json)_
- Amprenta artefactului: `MANIFEST.json` → `fingerprint_sha256` =
  `02571944523f2e41f31f61829fd59bd771f053500b82ba9df8048dddacf92d99`
- Probe: `rapoarte/BO-A01/VAL2-01/probe-ui/`
- Contract comun: `coordonare/contracte/bo.package.v1/`
