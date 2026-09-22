# PROGRES — BO-A01 (BOAgents Valul 1)

Agent: **BO-A01** · Repository: `Ravasi-Energy/BOAgents` · Branch: `bo/val1-a01-agents`
Base SHA: `622747c5221e70c2db629027bb0390c630ab7bc8` (`main`)

## Stadiu: TESTAT_LOCAL — toate verificările locale verde

### Finalizat

- **BA-01 reprodus și reparat** — `settings/layout.tsx` importa
  `@/components/settings/SettingsShell`, componenta lipsea de pe `main`.
  Reprodus prin `next build` (echec pe `/settings`), remediat cu
  `components/settings/SettingsShell.tsx` (tab nav cu `aria-current`, 44px).
- **Backend `openexecutive.bo`** — modul nou, izolat, cu DB propriu
  (`bo_agents.db`, `BOAGENTS_DB_PATH`):
  - `bo/identity.py` — identitate din `x-caller-email` (stampilat de proxy-ul
    NextAuth, nespoofabil) + `x-api-key` (serviciu) + fallback dev; roluri
    viewer < operator < admin (`BO_ADMIN_EMAILS` + `is_principal` din roster);
    tenant din `BO_TENANT_ID`, **niciodată** din payload; `x-bo-tenant`
    divergent → 403 fără divulgare; `OE_PUBLIC_DEPLOYMENT` fără identitate → 401.
  - `bo/settings/` — registry cu cele 5 chei Valul 1 (display_name, language
    ro/en, timezone IANA, max_steps 1–500, retention_days 1–3650), store cu
    CAS `expected_version` → 409, `origin` tenant/default, audit
    `bo_setting_change`.
  - `bo/bots/` — gramatică închisă `bo.bobot.v1` (pydantic `extra="forbid"`),
    evaluator de predicate în 3 valori (TRUE/FALSE/UNKNOWN; input lipsă =
    UNKNOWN, nu false), ciclu ciornă→publicare (versiune activă imuabilă),
    simulator determinist cu `plan_hash` reproductibil, retenție pe
    `kind='simulation'` cu cascade, exemplul sintetic `heartbeat_stale`.
  - `bo/telemetry/` — validare strictă `bo.telemetry.v1` (câmpuri închise,
    denylist prompt/secret/date personale), adaptor injectabil **oprit
    implicit** (emit() renunță înainte să construiască plicul), transporturi
    Null/Buffered/Http; identitatea stampilată de adaptor.
  - `api/routes/bo.py` — `/bo/settings`, `/bo/bots` (+publish/simulate/runs),
    `/bo/runs/{id}`, `/bo/telemetry/status|validate`; erori mapate central.
- **UI nouă** (română, profil de tokens izolat `.bo-scope`, reversibil):
  `styles/bo.css` (2 teme, Plus Jakarta Sans local woff2, breakpoints
  700/1100, butoane ≥44px, inputuri ≥40px, text ≥12px, focus-visible),
  `IconBO` (registru Lucide), `lib/bo.ts` (client tipat cu `BoApiError`),
  pagini: `/settings/bo`, `/bo/bots`, `/bo/bots/nou`, `/bo/bots/[id]`,
  `/bo/runs/[runId]`. Stări explicite: loading/gol/eroare/forbidden/
  unconfigured; pill-urile poartă text (nu numai culoare).
- **Fixture-uri + contract** — `fixtures/bo/telemetry/bo.telemetry.v1.schema.json`
  + 5 valide + 7 invalide; `fixtures/bo/bots/heartbeat_stale.bobot.json`
  (paritate cu `examples.HEARTBEAT_STALE`, testată).
- **Docs** — secțiunea `bo_agents` în architecture (sections.py + UI +
  prebuilt JSON), `.env.example` completat cu `BO_*`.

### Măsurători efectuate

- Contrast tokeni BO: **14 perechi × 2 teme**, toate ≥4.5:1 text / ≥3:1
  componente, după remedierea `--bo-line-strong` dark (2.29→3.50:1).
- `next build`: verde (toate rutele BO compilează; `/settings` reparată).
- `ruff check` + `mypy` pe `openexecutive/bo` + rute + teste: curat.
- Teste noi: **106** (predicates 35, settings 21, bots 22, telemetry 15,
  routes 13) — toate verde.
- **Suite backend completă: 3569 passed, 1 skipped** (baseline 3463 + 106 noi;
  zero regresii; singura reparație necesară a fost adăugarea `bo_agents` în
  lista canonică din `test_architecture_facts.py` — mecanismul de lock-in
  al secțiunilor).
- Teste node UI: **51 passed**.
- Smoke pe build-ul produs (`next start`): rutele `/bo/*` și `/settings/bo`
  servesc (302→signin fără sesiune, conform guardului existent); bundle CSS
  conține regulile BO (media 700/1100, pointer:coarse, 44px, font Jakarta).
- Ledger Anvil: baseline (BA-01 eșec build + 3463 verde) + 9 înregistrări
  `after`/`review`, toate `passed=1`.

### Limite cunoscute (Valul 1)

- BoBots `kind=AI|MIXED` există ca definiții dar nu pot fi simulate (refuz 422).
- `notify` acceptă doar `channel=internal`; niciun efect extern, nicio execuție.
- Adapterul HTTP de telemetrie e implementat dar neactivat — cerere de
  contract/ingestion către BOGuardian (BO-A02) rămâne pentru integrator.
- Capturi de ecran pe viewporturi (390/768/1440, edge 699/1099): măsurătorile
  de contrast/breakpoint sunt în CSS + verificate numeric; capturile vizuale
  rămân de produs la review-ul Codex (necesită instanță cu backend pornit).
