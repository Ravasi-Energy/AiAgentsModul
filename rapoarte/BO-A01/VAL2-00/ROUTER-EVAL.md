# Evaluare interfață router (BO-R03) — VAL2-00, separat de runtime

Mandat: evaluarea separată a interfeței routerului, **fără activare în
runtime**, fără instalare MetaHarness/Ruflo. Livrabilul este
`prototypes/bo_package/router_interface.py` — contract I/O (dataclasses +
reguli de comportament), fără implementare de selecție.

## Ce am preluat din studiu și ce nu

Sursa: `STUDIU-METAHARNESS-RUFLO.md`, router MetaHarness
`packages/router/src/index.ts` + `train.ts` @
`d5833dc6512ac1adeeef91a331c29055cd8a4dbb` (MIT).

Idei reținute în contract:
- selecție după cost + calitate, cu **restricțiile tenantului aplicate înainte**
  de ranking;
- prag ratat = `REFUSE` cu `best_effort` informativ și `requires_escalation`,
  **nu** „cel mai bun candidat prezentat drept acceptat" (defectul semnalat:
  upstream poate întoarce `metBar=false` — la noi e refuz explicit);
- calitatea vine din evaluări proprii cu `eval_evidence_ref`, nu din afirmații
  de economii nedemonstrate;
- `policy_ref` + `correlation_id` pe orice decizie — auditabil în telemetrie.

Nu am preluat: instalarea harnessului, adaptorii, CLI-ul, antrenarea/feedbackul
pe date de tenant (memoria și telemetria rămân separate între clienți).

## Ieșirile de refuz propuse

`NO_CANDIDATE`, `ALL_DISABLED`, `BUDGET_EXCEEDED`, `QUALITY_BAR_UNMET`,
`POLICY_DENIED`, `STALE_EVALUATION` — fiecare este verdict de primă clasă, nu
fallback silențios.

## Ce rămâne pentru VAL2-01+

- implementarea efectivă a selecției în spatele unui adaptor înlocuibil;
- măsurarea costului real și a pragului de calitate pe sarcini proprii;
- legătura cu setările propuse (`bo.router.*`, catalog de modele permise);
- dovada că REFUSE oprește execuția, nu doar o etichetează.

Interfața poate fi ratificată/adjustată prin artefactul versionat; nu e
activată și nu schimbă comportamentul produsului.
