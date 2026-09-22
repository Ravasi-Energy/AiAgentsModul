# PREDARE — BO-A01 / VAL2-01

Stare: **TESTAT_LOCAL** (nu acceptat — verificarea independentă aparține
Codex).

| | |
|---|---|
| Repository | `Ravasi-Energy/BOAgents` |
| Branch | `bo/val2-01-a01-packages` |
| baseSHA | `46424b5c865bde7e74d3c9f56ea148fc0ae8a4c0` |
| headSHA | `214b1f8` (implementare) + raportul — vârf la predare: `git rev-parse bo/val2-01-a01-packages` |
| PR | https://github.com/Ravasi-Energy/BOAgents/pull/5 (draft, bază `main`) |
| Artefact comun | `coordonare/contracte/bo.package.v1/` (cu SHA256SUMS) |

## Cerințe acoperite

- Integrare `bo.package.v1` în runtime: modul `openexecutive/bo/packages/`
  (contract/canon/signing/registry/verify/store/service) — import →
  verificare → carantină → draft, **fără activare**.
- Toate remediile din decizia de contract: limite înainte/în timpul citirii,
  chei duplicate `DUPLICATE_KEY`, `NaN`→`INVALID_MANIFEST`, `manifest.json`
  imbricat = artefact ordinar, reimport idempotent pe `manifest_digest`,
  conflict pe manifest diferit, downgrade cu aprobare legată
  tenant/digest/from/to, expirare, consum unic.
- Trust store `bo.package.registry.v1` separat — cheia nu vine din manifest;
  `version`/`policyVersion`; validare structurală la salvarea setării.
- Setări noi (4): `enabled` (off), `max_package_bytes`, `trust_store_json`,
  `rollback_requires_approval` — CAS + audit + tenant scoping.
- API: 6 endpoint-uri noi + mapping erori; `packages:write` = admin.
- UI: `/bo/packages` (RO, `.bo-scope`), editor boolean pentru setări,
  navigație.
- Artefact comun predat în `coordonare/contracte/bo.package.v1/` pentru
  A02/A03: contract, schemă, BO-C14N-v1, 24 fixture-uri + expected +
  context, 21 vectori canonici, SHA256SUMS, regen.py.

## Teste și rezultate reale

| Verificare | Rezultat |
|---|---|
| `pytest tests/unit` complet | **3615 passed, 1 skipped** |
| `test_bo_packages.py` | 41 passed |
| ruff (fișiere atinse) | curat |
| mypy (fișiere atinse) | curat (13 fișiere) |
| `next build` | verde, `/bo/packages` inclus |
| Probe UI | 11 probe, 0 overflow, 2 teme, tastatură — `probe-ui/REZULTATE.md` |
| `regen.py` artefact | 24 fixture, 21 vectori, clasificare confirmată |
| Ledger Anvil | 2 baseline + 9 after/review, toate `passed=1` |

## Defecte găsite de proba reală și remediate

1. Trust store JSON indentat respins (`\n`) → validatorul acceptă `\n`/`\t`.
2. Rând REJECTED ocupa slotul UNIQUE → `IntegrityError`/500 la import valid
   → index unic parțial pe rânduri vii + tratament de cursă în
   `insert_import`.
3. Idempotența pe `artifact_set_digest` putea întoarce verdict ACCEPT pentru
   un manifest modificat neverificat → idempotent = `manifest_digest`
   identic; manifest diferit = `VERSION_CONFLICT`.

## Migrare / rollback

- Tabel nou `bo_package_imports` + `bo_package_approvals` în `bo_agents.db`
  (DDL idempotent, `initialize_db`). Nu există instalări anterioare ale
  acestor tabele — nu e nevoie de migrare.
- Revenire: revert commit + șterge `bo_packages/` quarantine dir (implicit
  `./bo_packages`, ignorat de git) — niciun efect extern nu există.
- Setarea `bo.packages.enabled=false` (implicit) dezactivează complet
  importul.

## Limite

- Import dintr-un director local al serverului (nu upload de fișiere).
- Verdictul nu e aprobare de execuție; activarea nu există în acest lot.
- UI pentru trust store = input text (funcțional; textarea dedicată poate
  veni ulterior).
- Cheile din `keys.json` sunt sintetice, doar pentru fixture-uri.

## Artefacte

- Bundle: `agenti/BO-A01/bo-a01-val2-01.bundle` _(SHA256 în MANIFEST-VAL2-01.json și coordonare/rapoarte/BO-A01/PROGRES.md)_
- Probe: `rapoarte/BO-A01/VAL2-01/probe-ui/` (11 capturi + rezultate)
- Contract comun: `coordonare/contracte/bo.package.v1/` (SHA256SUMS inclus)
