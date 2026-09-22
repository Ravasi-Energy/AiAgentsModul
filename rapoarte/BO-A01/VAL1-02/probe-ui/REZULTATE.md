# Probe vizuale UI — VAL1-02 (rezultate măsurate)

Data: 22.09.2026. Stivă reală: `next start :3100` (build de producție) +
backend FastAPI :8000, sesiune NextAuth JWT reală (`probe@bo.dev`, admin).
Browser: Google Chrome headless via playwright-core 1.63.
Toate paginile verificate randează `.bo-scope` și **fără overflow orizontal**
la niciun viewport.

## Capturi — 5 pagini × {390, 768, 1440} × {dark, light} = 30

| Pagină | 390 | 768 | 1440 | teme | overflow |
|---|---|---|---|---|---|
| `/settings/bo` | ✓ | ✓ | ✓ | ambele | nu |
| `/bo/bots` | ✓ | ✓ | ✓ | ambele | nu |
| `/bo/bots/nou` | ✓ | ✓ | ✓ | ambele | nu |
| `/bo/bots/[id]` | ✓ | ✓ | ✓ | ambele | nu |
| `/bo/runs/[runId]` | ✓ | ✓ | ✓ | ambele | nu |

## Edge breakpoints (măsurat pe proprietăți computed, tema dark)

| Prag | sub | peste | comută |
|---|---|---|---|
| 699→700 | grid 1 col, padding-x 16px, titlu 20px | grid 2 col, padding-x 24px, titlu 22px | ✓ |
| 1099→1100 | padding-x 24px | padding-x 40px | ✓ |

Capturi: `edge-699/700/1099/1100-dark.png`.

## Tastatură (`/settings/bo`, 768, dark)

14 opriri Tab consecutive; fiecare element focalizat are contur vizibil
(`focus-visible`); ordinea ajunge la controalele de formular
(input/select/button). Captură: `keyboard-focus-768-dark.png`.

## Stări de eroare (768, dark)

| Probă | Rezultat |
|---|---|
| `/bo/bots/bot_nu_exista` | „BoBot negăsit · Definiția nu există" + acțiune „Înapoi la listă" |
| `/bo/runs/run_inexistent` | „Rularea nu există" |
| Conflict CAS real (două sesiuni, salvări concurente) | banner ambrin: „Altă modificare s-a salvat între timp. Reîncarcă pagina pentru valoarea curentă." |

Capturi: `error-bot-inexistent-768-dark.png`, `error-run-inexistent-768-dark.png`,
`error-cas-768-dark.png`.

## Metodă (reproductibil)

Backend: `uvicorn openexecutive.api.main:app :8000` cu
`BO_TENANT_ID=tenant-probe`, `BO_ADMIN_EMAILS=probe@bo.dev`,
`BOAGENTS_DB_PATH=/tmp/bo-probe.db`, `BACKEND_SHARED_SECRET=probe-secret`.
UI: `next start :3100` cu `AUTH_SECRET` generat, `ALLOWED_EMAILS=probe@bo.dev`,
`AUTH_TRUST_HOST=true`. Sesiune JWT bătută cu `next-auth/jwt encode`
(cookie `authjs.session-token`) — fără modificări de cod pentru probe.
Driver: `/tmp/bo-probe/probe.mjs` + `probe2.mjs` (playwright-core, canal chrome).
