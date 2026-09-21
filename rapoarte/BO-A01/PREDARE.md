# PREDARE — BO-A01 · BOAgents Valul 1

| | |
|---|---|
| Sesiune | **BO-A01** (Devin, model SWE-2 Max) |
| Repository | `Ravasi-Energy/BOAgents` (privat) |
| Branch | `bo/val1-a01-agents` |
| baseSHA | `622747c5221e70c2db629027bb0390c630ab7bc8` (`main`) |
| headSHA implementare | `8f0398a62f33f10786ba0037bc0ced31ec201a97` |
| headSHA ramură | tipul ramurii după commitul acestui raport — valoarea exactă și hash-ul bundle-ului sunt în `MANIFEST.json` livrat alături |
| Stare finală | **TESTAT_LOCAL** — Codex verifică independent și decide acceptarea |

Fără push, fără PR, fără merge. Nicio modificare în BOGuardian, Hire sau ERP.

## Tabel cerințe

| ID | implementare | probă | rezultat | SHA | limită |
|---|---|---|---|---|---|
| BA-01 | `components/settings/SettingsShell.tsx` nou; `next build` trece pe `/settings` | build produs | reparat | 8f0398a | — |
| BO-SET-001 | `bo/settings/{registry,store}.py` + `/bo/settings` API; 5 chei, CAS, origin, audit | `test_bo_settings.py` (21) | verde | 8f0398a | doar cheile Valul 1 |
| BO-BOT-001 | `bo/bots/{models,store}.py`, gramatică `bo.bobot.v1`, draft/publish | `test_bo_bots.py` (22) | verde | 8f0398a | `AI`/`MIXED` definibile, nesimulabile |
| BO-BOT-002 | `bo/bots/predicates.py` — 3 valori, input lipsă→UNKNOWN, limite de resurse | `test_bo_predicates.py` (35) | verde | 8f0398a | gramatică închisă, fără eval |
| BO-BOT-003 | `bo/bots/service.py` — dry-run determinist, `plan_hash`, retenție | teste simulate + determinism | verde | 8f0398a | zero efecte externe, by design |
| Telemetrie | `bo/telemetry/{schema,adapter}.py` + `fixtures/bo/telemetry/` | `test_bo_telemetry.py` (15) | verde | 8f0398a | oprit implicit; HTTP neactivat |
| UI Setări | `/settings/bo` — editoare tipizate, CAS, stări, RO | build + smoke | verde | 8f0398a | capturi vizuale la review Codex |
| UI BoBots | `/bo/bots`, `/nou`, `/[id]`, `/bo/runs/[runId]` | build + smoke | verde | 8f0398a | idem |
| DESIGN | `styles/bo.css` — tokens izolați, 2 teme, 700/1100, ≥44px, Jakarta local | script contrast: 14 perechi × 2 teme ≥4.5/3.0 | verde | 8f0398a | — |
| Identitate | `bo/identity.py` — tenant server-side, roluri, fail-closed public | `test_bo_routes.py` (13) | verde | 8f0398a | — |

## Teste executate (comenzi reale)

- `uv run pytest tests/unit/ -k bo_` → **106 passed** (predicates 35, settings 21, bots 22, telemetry 15, routes 13)
- `uv run pytest tests/` (suita completă) → **3569 passed, 1 skipped** în 152s (baseline 3463 → zero regresii)
- `uv run ruff check openexecutive/bo routes/bo.py` → curat; `mypy` → 0 erori
- `npm run build` (packages/ui) → verde; `/settings` reparată, toate rutele BO compilează
- `npm test` (packages/ui, node:test) → **51 passed**
- Smoke `next start` pe build-ul produs → rutele BO servesc (302→signin fără sesiune); CSS produs conține regulile BO
- Contrast: script WCAG pe ambele teme — toate perechile ≥4.5:1 text / ≥3:1 componente (remediat `--bo-line-strong` dark 2.29→3.50)
- Ledger Anvil (`/tmp/anvil_sql.py`): 2 baseline + 9 after/review, toate `passed=1`

## Migrări / pași de rulare

- Tabele noi în `bo_agents.db` (creat la `lifespan`); variabile noi documentate în `.env.example` (`BO_TENANT_ID`, `BO_ADMIN_EMAILS`, `BOAGENTS_DB_PATH`, `BO_TELEMETRY_*`) — toate cu defaulturi sigure.
- Dependență UI nouă: `lucide-react@^0.545.0`; 2 fonturi woff2 locale în `public/fonts/`.

## Limite rămase

- `kind=AI|MIXED`: refuz 422 la simulare (Valul 1 e numai BOT determinist).
- `notify` doar `channel=internal`; niciun efect extern, nicio execuție reală.
- Endpointul HTTP de telemetrie e implementat dar neactivat — integrarea cu ingestion BOGuardian rămâne la integrator.
- Capturi ecran pe viewporturi: contrastul/breakpoint-urile sunt verificate numeric în CSS; capturile vizuale necesită instanță cu backend — de produs la review.
- Test preexistent roșu pe `main` (nemințit de mine): `test_chat_committee.py::test_chat_with_committee_streams_phases_and_revised_text` — eșuează și pe baseSHA.

## Procedura de revenire

`git checkout 622747c` sau `git revert 8f0398a` — modificarea e aditivă; tabelele BO stau într-un DB separat care poate fi șters fără impact asupra `episodic_memory.db`. Stratul UI BO e izolat sub `.bo-scope`; scoaterea importului `@import "../styles/bo.css"` + intrările din `navConfig.ts` + directoarele `app/bo`, `app/settings/bo`, `components/bo`, `components/settings` revine complet.

## Artefact

- Bundle git `bo-a01-val1.bundle` (base..tip) + `MANIFEST.json` cu SHA256 și headSHA — livrate în sesiune, în afara repo-ului.
- Acest raport este commitat separat peste headSHA-ul implementării; nu modifică cod.
