# SPEC-ROUTER-OBSERVE — router cost/calitate în mod observare (BO-R03)

Document de pregătire, **nu autorizare de implementare VAL3**. Nu există
cod runtime, nu se schimbă modelul activ, nu se fac apeluri la provideri,
nu se fixează modele sau prețuri în cod. Porneste de la contractul I/O
propus în `prototypes/bo_package/router_interface.py` (VAL2-00) și de
mandatul BO-R03 din `coordonare/PLAN-INTEGRARE-BO-V2.md`.

## 1. Scop și modul observare

Routerul calculează o decizie `ROUTE`/`REFUSE` pentru fiecare solicitare
de model și o **înregistrează ca observație auditabilă**, fără a o aplica.
Modelul activ rămâne cel configurat astăzi în sistem — observarea produce
date pentru calibrare, nu schimbă comportamentul.

- decizia observată = ce s-ar fi ales **dacă** routerul ar fi fost activ;
- fiecare observație poartă `policy_ref` + `correlation_id` + rezultatul
  efectiv al rutei reale (modelul folosit, costul real, calitatea rezultatului)
  → comparație declarativ vs real;
- Guardian observă deciziile (telemetrie); Hire își păstrează politica de
  autorizare — routerul nu o înlocuiește.

## 2. Intări

| Câmp | Sursă | Observație |
|---|---|---|
| `tenant_id` | contextul cererii | obligatoriu; izolare absolută |
| `task_kind` | caller (`chat`, `workflow_step`, `agent_step`, `classification`) | din contractul VAL2-00 |
| `candidates` | catalogul de modele administrat (Setări) | nu din corpul cererii utilizatorului |
| `constraints` | politica tenantului (Setări) | `allowed_providers`, `allowed_models`, `max_cost_per_call`, `min_quality`, `max_latency_ms`, `require_evaluated` |
| `data_policy` | politica tenantului | regiune/date — ex. rezidența datelor admisă |
| `budget_state` | contorul de buget al tenantului | costul acumulat vs plafon |
| `policy_ref` | versiunea politicii aplicate | auditabil |
| `correlation_id` | cererea | leagă observația de rulare |

## 3. Ieșiri

`RouteDecision` (nemodificat față de VAL2-00):
`decision ∈ {ROUTE, REFUSE}`, `model_id`, `refusal`,
`met_bar`, `applied_constraints`, `best_effort` (informativ),
`requires_escalation`, `reason`.

Coduri de refuz: `NO_CANDIDATE`, `ALL_DISABLED`, `BUDGET_EXCEEDED`,
`QUALITY_BAR_UNMET`, `POLICY_DENIED`, `STALE_EVALUATION` — fiecare
verdict de primă clasă.

**În mod observare** se adaugă înregistrarea: decizia calculată +
modelul activ folosit efectiv + `met_bar` + abaterea estimată de
cost/calitate. Nicio rutare reală nu este modificată.

## 4. Filtrare înainte de scor (ordine fixă)

1. **Tenant** — candidatul trebuie permis tenantului; altfel eliminat.
2. **Regiune/politică de date** — providerul/regiunea trebuie admisă de
   `data_policy`; datele nu părăsesc perimetrul declarat.
3. **Capabilități** — modelul trebuie să acopere cerințele task-ului
   (ex. fereastra de context, tipuri de intrare).
4. **Buget** — `max_cost_per_call` și plafonul acumulat al tenantului.
5. **Disponibilitate** — `status ∈ {available}`; `disabled`/`degraded`
   sunt eliminate (`ALL_DISABLED` dacă rămâne zero).

Doar după filtrare: **scor** pe calitate (`quality_score` din evaluări
proprii cu `eval_evidence_ref`) + cost real declarat. Modelele fără
dovadă de evaluare nu sunt eligibile când `require_evaluated=true`.

## 5. Politici administrabile

Setări propuse `bo.router.*` (același model ca în
`SETARI-COMPORTAMENT.md`: scope tenant, validare, CAS `expected_version`,
audit, `apply_mode` sincer):

| Cheie propusă | Tip | Rol în router |
|---|---|---|
| `bo.router.observe_enabled` | bool | pornește doar înregistrarea deciziilor |
| `bo.router.allowed_providers` | listă | filtru tenant/provider |
| `bo.router.allowed_models` | listă | filtru fin pe modele |
| `bo.router.max_cost_per_call` | număr | filtru buget per apel |
| `bo.router.min_quality` | număr | prag calitate (`QUALITY_BAR_UNMET`) |
| `bo.router.eval_max_age_days` | int | dovada de evaluare expirată → `STALE_EVALUATION` |

Nicio valoare nu e îngropată în cod; catalogul de modele e date
administrate, nu constante.

## 6. `met_bar=false`, fallback, date stale

- `met_bar=false` → `REFUSE` + `requires_escalation` după politică
  (`STOP` sau `ESCALATION_REQUIRED`) — niciodată „cel mai bun candidat
  prezentat drept acceptat".
- **Fallbackul nu crește drepturile**: un model de rezervă trebuie să
  treacă aceleași filtre (tenant, date, capabilități, buget); nu poate
  depăși bugetul și nu poate evada restricțiile `allowed_*`.
- **Date stale**: evaluare mai veche de `eval_max_age_days` →
  `STALE_EVALUATION`; catalog/providere necunoscut → eliminat din
  candidați, nu tratat ca eligibil. Lipsa datelor nu devine scor 0
  artificial — `quality_score=None` înseamnă neevaluat, nu slab.

## 7. Seturi de calibrare/test (separate)

- **Calibrare**: sarcini BO reprezentative cu răspunsuri etalon — fixează
  pragurile `min_quality` și mapează `quality_score` la o scară măsurată.
- **Test**: set separat, nefolosit la calibrare — validează că deciziile
  se mențin pe sarcini nevăzute.
- Cele două seturi nu se amestecă; ambele sunt **sintetice/anonimizate**,
  fără date de tenant. Fiecare exemplu poartă versiune+dată; evaluările
  se redatează la schimbarea catalogului.

## 8. Exemple sintetice

**Pozitiv (ROUTE):** tenant `acme`, task `workflow_step`, candidați
`{model-A: calitate 0.91, 3$/Mtok, available, evaluat la zi}` —
politica permite providerul, buget ok → `ROUTE model-A`, `met_bar=true`.

**Negative (REFUSE):**
- `model-A` unic, `quality_score 0.61` < `min_quality 0.8` →
  `QUALITY_BAR_UNMET`, `best_effort=model-A`, `requires_escalation=true`.
- Toți candidații `degraded` → `ALL_DISABLED`.
- Providerul unic nu e în `allowed_providers` al tenantului →
  `POLICY_DENIED` — filtrat înainte de orice scor.
- Evaluarea lui `model-A` are 120 zile > `eval_max_age_days 90` →
  `STALE_EVALUATION`.
- Fallback propus `model-X` peste `max_cost_per_call` → respins de filtru;
  fallbackul nu relaxează constrângerile.

## 9. Criterii de acceptare (pentru implementarea viitoare)

1. Pe setul de test, fiecare decizie observată respectă: filtrare
   completă înainte de scor; niciun `ROUTE` cu `met_bar=false`.
2. Rejucarea aceluiași intrări + aceeași politică → decizie identică
   (determinist, fără apeluri live).
3. `REFUSE` raportează codul corect și `best_effort` doar informativ.
4. Observațiile sunt auditabile: `policy_ref`, `correlation_id`,
   constrângeri aplicate, comparație cu ruta reală.
5. Politicile se administrează prin Setări (validare + CAS + audit), nu
   prin editare de fișiere sau cod.

## 10. Câmpuri certe vs decizii deschise

**Cert (din contractul VAL2-00 + mandat BO-R03):** forma I/O
(`RouterRequest`, `RouteDecision`, coduri `RefusalCode`), ordinea
filtrării, `met_bar=false → REFUSE`, fallback fără lărgirea drepturilor,
`require_evaluated`, separarea calibrare/test, administrare prin Setări.

**Deschis (pentru coordonator):**
- scara exactă `quality_score` (0–1 vs 0–100) și metodologia de evaluare
  a sarcinilor BO;
- metrica de „cost real" la observare (facturat vs estimat din tokeni);
- cine emite catalogul de modele (A01 propus, dar validarea inventarului
  e teritoriu BO-R05/A02);
- granularitatea `data_policy` (per tenant vs per task);
- unde locuiește contorul de buget (BOAgents vs Guardian).
