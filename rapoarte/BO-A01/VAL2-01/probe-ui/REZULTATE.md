# Probe vizuale UI — VAL2-01 (rezultate măsurate)

Data: 23.09.2026. Stivă reală: `next start :3100` (build de producție) +
backend FastAPI :8000, sesiune NextAuth JWT reală (`probe@bo.dev`, admin via
`BO_ADMIN_EMAILS`). Browser: Google Chrome headless via playwright 1.63.

Date seed-uite prin API real: `bo.packages.enabled=true`,
`bo.packages.trust_store_json` = `registry.example.json` din artefactul
comun, import `valid-bobot` (QUARANTINED) + `incompatible-host` (REJECTED) +
o aprobare activă de downgrade.

## `/bo/packages` — 10 capturi + tastatură

| Probă | Rezultat |
|---|---|
| 390 × dark / light | ✓ fără overflow |
| 768 × dark / light (+ verdict expandat) | ✓ fără overflow |
| 1440 × dark / light | ✓ fără overflow |
| edge 699 / 700 / 1099 / 1100 (dark) | ✓ fără overflow |
| tastatură (12 × Tab, 768 light) | focus pe A/BUTTON, vizibil |

Pagina afișează: tabel importuri (pachet, versiune, emitent, stare
carantină/respins, digest, dată), panou verdict expandabil
(manifestDigest, checkedAt, politica, sursa), card import cu calea sursă,
secțiunea aprobărilor (activă/consumată/expirată/revocată) cu formular de
creare și revocare — toate în română, pe tokens `.bo-scope`.

## `/settings/bo` — setări boolean noi

Captură `settings-bo-pachete.png`: cele 9 setări se randează; cele două
chei boolean VAL2-01 (`bo.packages.enabled`,
`bo.packages.rollback_requires_approval`) primesc select Oprit/Pornit
(remediat în acest lot — înainte ar fi fost input text → 422 la salvare).

## Defecte găsite de probă (remediate în acest lot)

1. `bo.packages.trust_store_json` respingea JSON indentat (`\n` = caracter
   de control) → `_validate_trust_store` acceptă acum `\n`/`\t`.
2. O respingere persistată ocupa slotul UNIQUE `(tenant, pkg, versiune,
   artifact-set)` → import valid ulterior dădea 500 IntegrityError.
   Remediat cu index unic parțial `WHERE status != 'REJECTED'` +
   tratament de cursă în `insert_import`.
3. Idempotența se lega de `artifact_set_digest` — un manifest diferit cu
   aceleași artefacte (ex. capabilități schimbate) ar fi fost returnat
   „idempotent" fără re-verificare. Idempotent = acum același
   `manifest_digest`; manifest diferit la aceeași versiune →
   `VERSION_CONFLICT`.
