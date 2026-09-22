# PROGRES — BO-A01 / VAL2-01

Lot: integrarea contractului `bo.package.v1` în runtime BOAgents +
artefactul comun `coordonare/contracte/bo.package.v1/`.

Checkout: `agenti/BO-A01/boagents-val2-01`, ramură `bo/val2-01-a01-packages`.
Base: `46424b5` (origin/main=VAL1-02 merged + delta prototip VAL2-00).

## Stare: TESTAT_LOCAL

### Livrat

- **`openexecutive/bo/packages/`** — modul runtime complet:
  `contract.py` (schemă închisă, limite înainte/în timpul citirii, chei
  duplicate, NaN, traversal), `canon.py` (BO-C14N-v1), `signing.py`
  (Ed25519), `registry.py` (trust store `bo.package.registry.v1`,
  `version`+`policyVersion`), `verify.py` (9 pași, verdict comun),
  `store.py` (SQLite, tenant-scoped, index unic parțial pe rânduri vii),
  `service.py` (import→verificare→carantină→draft, drift-check la copiere
  și la promovare, aprobări legate tenant/digest/versiuni cu expirare și
  consum unic).
- **Setări** — `bo.packages.enabled` (oprit implicit), `max_package_bytes`,
  `trust_store_json` (validat structural la salvare, JSON indentat legal),
  `rollback_requires_approval`.
- **API** — `GET /bo/packages`, `POST /bo/packages/import`,
  `GET /bo/packages/{id}`, `POST /bo/packages/{id}/promote`,
  `GET/POST /bo/packages-approvals`, `POST /bo/packages-approvals/{id}/revoke`;
  `packages:read`=viewer, `packages:write`=admin; `package_rejected` 422
  cu codul verificatorului; niciun endpoint nu divulgă cross-tenant.
- **UI** — `/bo/packages` (listă, verdict expandabil, import admin,
  aprobări cu creare/revocare), intrare în navigație, editor boolean pentru
  setări, icon `package` în registru.
- **Artefact comun** — `coordonare/contracte/bo.package.v1/`: contract,
  schemă, spec BO-C14N-v1, 24 fixture-uri, 21 vectori canonici,
  `expected.json` cu context, `registry.example.json`, `keys.json`
  sintetice, `SHA256SUMS`, `CHANGELOG`, `regen.py` (regenerare
  deterministă + auto-verificare cu verifierul runtime).
- **Docs** — `architecture/prebuilt/bo_agents.json` + `api.json`
  actualizate (modul nou, endpoints noi, 9 setări).

### Defecte găsite în probă/verificare și remediate

1. Trust store respingea JSON indentat (`\n` control char) → validatorul
   acceptă `\n`/`\t`.
2. Rând REJECTED ocupa slotul UNIQUE → 500 la import valid ulterior →
   index unic parțial `WHERE status != 'REJECTED'` + tratament de cursă.
3. Idempotența se lega de `artifact_set_digest` → manifest diferit cu
   aceleași artefacte putea întoarce ACCEPT neverificat → idempotent =
   același `manifest_digest`; manifest diferit la aceeași versiune =
   `VERSION_CONFLICT`.

### Verificări

- `test_bo_packages.py`: 41 teste (accept, matrice 13 refuzuri, downgrade
  cu aprobări legate, drift, canon, acces, rute HTTP).
- Suita `tests/unit` completă: vezi PREDARE.md.
- ruff + mypy curate pe toate fișierele atinse.
- `next build` verde; probe UI: 10 capturi `/bo/packages` + tastatură +
  edge viewports + settings booleans — `probe-ui/REZULTATE.md`.
- `regen.py` artefact comun: 24 fixture-uri, 21 vectori, clasificare
  auto-confirmată.

### Limite declarate

- Nu există activare/execuție — stările ajung la DRAFT; verdictul nu e
  aprobare de execuție.
- `import` acceptă o cale pe server (decizie de produs pentru acest lot —
  upload de fișiere rămâne pentru un lot viitor).
- Editorul UI pentru `trust_store_json` este input text — funcțional, dar
  JSON-ul de ~1.7 KiB e mai comod de gestionat în afara UI; textarea
  dedicată poate veni într-un lot viitor.
- Aprobările sunt create prin API/UI de admin; nu există încă flux de
  semnare externă a aprobării.
