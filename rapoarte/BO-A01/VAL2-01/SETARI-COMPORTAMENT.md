# Comportamentul Setărilor BOAgents — referință de integrare (FIN-02)

Document de sprijin pentru A02 (Guardian) și A03 (Hire): cum funcționează
Setările implementate în BOAgents, cu trimiteri la codul și testele reale.
Nu propune un motor nou și nu atinge schema `bo.package.v1` înghețată.

Surse de adevăr:
`packages/core/openexecutive/bo/settings/registry.py` (REGISTRY — 9 chei),
`.../settings/store.py` (persistență + CAS), `.../bo/identity.py`
(capabilități/roluri), `openexecutive/api/routes/bo.py` (`/bo/settings*`),
`packages/ui/src/app/settings/bo/page.tsx` (suprafața), teste:
`tests/unit/test_bo_settings.py`, `test_bo_packages.py`.

## Scope și roluri

- Scope **tenant**: o suprascriere per `(tenant, key)` în `bo_settings`
  (SQLite, `bo_agents.db`). Niciun câmp global în acest lot.
- Capabilități (`_CAP_MIN_ROLE`): `settings:read` → `viewer`,
  `settings:write` → `admin`. Rolul vine din identitatea rezolvată
  (`x-caller-email` + `BO_ADMIN_EMAILS`/roster principal; fallback `dev`
  doar fără secret partajat și fără deployment public).
- Izolare: tenantul configurat al instalației e singurul acceptat; orice
  indiciu diferit (`x-bo-tenant`, `?tenant=`) → 403 `tenant_mismatch`.

## Validare

Fiecare cheie are validator propriu în `REGISTRY` (`type` + `validate`):
text (1–80), enum (`ro|en`), timezone IANA, int cu interval, bool strict,
JSON structural pentru `trust_store_json`. Cheie necunoscută → 404
`unknown_setting`; valoare invalidă → 422 `invalid_value` cu mesaj
explicit (handler `_bo_invalid`). Validarea rulează **înainte** de orice
scriere — o respingere nu atinge stocarea.

## Salvare cu `expected_version` (CAS per cheie)

```
PUT /bo/settings/{key}   { "value": ..., "expected_version": <int> }
```

- `expected_version` e obligatoriu (`ge=0`); `0` = cheia nu a fost
  suprascrisă niciodată.
- UPDATE condiționat pe versiune în `BEGIN IMMEDIATE`; scriitorul învechit
  pierde determinist → 409 `version_conflict`
  (`test_expected_version_conflict`, `test_conflict_does_not_write`).
- Răspuns: `{result: "SAVED", apply_mode, applied, setting{...}}` —
  `applied` este `true` doar pentru `IMMEDIATE` (vezi mai jos).

## Conflict și audit

- 409 `version_conflict` cu detaliu `expected_version=X dar versiunea
  curentă este Y` — clientul re-citește și retrimite.
- Fiecare salvare scrie `bo_setting_change` în auditul global
  (`store._audit_change`): tenant, cheie, versiune nouă, `old`/`new` —
  **redactate** (`"<redacted>"`) dacă `sensitivity != "normal"`.
- Pentru `IMMEDIATE` se emite și telemetrie `ConfigApplied` prin adaptor
  (no-op dacă telemetria e oprită): `configVersion` opac + `appliedVersion`
  per cheie + `actorRef` opac (niciodată emailul).

## Valoare dorită vs activă (`apply_mode`)

`ApplyMode = IMMEDIATE | NEW_RUN | RESTART | MIGRATION` — tipul există;
în acest lot sunt folosite doar primele două:

- `IMMEDIATE`: efect instant; `applied=true` în răspuns.
- `NEW_RUN` (ex. `bo.bobot.simulation.max_steps`): valoarea salvată se
  aplică doar rulărilor pornite după salvare; `applied=false` — UI-ul nu
  pretinde aplicare imediată.
- `RESTART`/`MIGRATION` sunt rezervate pentru configurații de
  infrastructură (necesită repornire/migrare): se afișează valoarea
  **dorită** vs **activă** sincer, fără a pretinde aplicare live.

## Persistență și origine

- `bo_settings(tenant, key, value, version, updated_at)` — rândul există
  doar pentru suprascrieri; valoarea efectivă = rând sau default-ul
  registrului.
- `GET /bo/settings` întoarce per cheie `value`, `origin`
  (`tenant` | `default`), `version`, plus `config_version` agregat.
- Supraviețuire verificată: `test_persistence_survives_reinit`;
  izolare: `test_tenant_isolation`.

## Referințe la secrete

- `sensitivity` pe fiecare chei; valorile non-`normal` apar redactate în
  audit. În acest lot toate cele 9 chei sunt `normal`.
- `bo.packages.trust_store_json` conține **doar chei publice** — nu e
  secret, dar e validat structural la salvare (registru malformat → 422).
- Regula pentru viitor: secretele reale se stochează prin referință
  (nume de secret / manager extern), nu în cleartext într-o cheie `normal`.

## Stări UI (DESIGN.md §„Stări")

Pagina `/settings/bo` randează fiecare cheie ca card cu: chip `implicit`
(origin `default`) vs `v{N}` (versiunea tenant), editor pe tip (text,
select pentru enum/bool, number pentru int), efectul declarat
(`effect_ro`), buton „Salvează" per cheie și erori de validare/409
afișate inline. Stările cerute de `coordonare/DESIGN.md` (date, eroare,
forbidden, unconfigured etc.) sunt acoperite de `.bo-scope`; probele:
`rapoarte/BO-A01/VAL2-01/probe-ui/` + `REZULTATE.md`.

## Recomandare pentru A02/A03

Reutilizați modelul: registry de chei cu validator + scope tenant + CAS
`expected_version` + audit + `apply_mode` sincer (dorită vs activă pentru
infra). Nu lăsați UI-ul să ocolească limitele/trustul/izolarea — scrierea
trece întotdeauna prin validare și rol.
