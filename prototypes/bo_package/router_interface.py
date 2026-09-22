"""Interfață propusă pentru routerul de modele (BO-R03) — CONTRACT, nu runtime.

Nu există implementare activă, nu există transport, nu se instalează
MetaHarness. Acest modul fixează doar forma I/O propusă, pentru evaluare și
pentru sincronizarea cu verificatorul A02. Inspirație conceptuală: routerul
MetaHarness (`packages/router/src/index.ts` @ d5833dc…, MIT) — fără cod preluat.
"""

from dataclasses import dataclass
from typing import Literal

# --- cerere ----------------------------------------------------------------

TaskKind = Literal["chat", "workflow_step", "agent_step", "classification"]


@dataclass(frozen=True)
class ModelCandidate:
    model_id: str                      # ex. "claude-sonnet-5"
    provider: str                      # catalogul de provideri permiși e politică
    price_in_per_mtok: float           # cost real declarat, nu afirmații upstream
    price_out_per_mtok: float
    quality_score: float | None        # din evaluări proprii; None = neevaluat
    context_window: int
    status: Literal["available", "disabled", "degraded"]
    eval_evidence_ref: str | None = None  # dovada scorului de calitate


@dataclass(frozen=True)
class RouteConstraints:
    allowed_providers: tuple[str, ...]      # restricție tenant — aplicată ÎNAINTE de selecție
    allowed_models: tuple[str, ...] | None  # None = tot catalogul providerilor permiși
    max_cost_per_call: float | None
    min_quality: float | None               # prag de calitate din setări
    max_latency_ms: int | None
    require_evaluated: bool = True          # modelele fără dovadă nu sunt eligibile


@dataclass(frozen=True)
class RouterRequest:
    tenant_id: str
    task_kind: TaskKind
    candidates: tuple[ModelCandidate, ...]
    constraints: RouteConstraints
    policy_ref: str                         # versiunea politicii aplicate (audit)
    correlation_id: str                     # pentru telemetrie/decizie auditată


# --- decizie ----------------------------------------------------------------

RefusalCode = Literal[
    "NO_CANDIDATE",          # lista goală / toate filtrate de restricții
    "ALL_DISABLED",          # candidați există, dar toți disabled/degraded
    "BUDGET_EXCEEDED",       # niciunul sub max_cost_per_call
    "QUALITY_BAR_UNMET",     # niciunul peste min_quality — cel mai bun e raportat
    "POLICY_DENIED",         # restricțiile tenantului nu admit nicio rută
    "STALE_EVALUATION",      # dovada de evaluare e expirată/lipsă și require_evaluated
]


@dataclass(frozen=True)
class RouteDecision:
    """Decizia routerului. REFUSE este o ieșire de primă clasă — un candidat
    sub prag NU este prezentat drept ales; `best_effort` e doar informație
    pentru escaladare, niciodată o rută acceptată implicit."""
    decision: Literal["ROUTE", "REFUSE"]
    model_id: str | None = None
    refusal: RefusalCode | None = None
    met_bar: bool = False                   # true doar dacă pragurile sunt atinse
    applied_constraints: tuple[str, ...] = ()  # ce restricții au fost aplicate
    best_effort: str | None = None          # candidat informativ la REFUSE
    requires_escalation: bool = False
    reason: str = ""                        # explicație structurată, fără secrete


# --- contract de comportament (testabil în VAL2-01) -------------------------
#
# 1. Filtrare → selecție → prag: restricțiile tenantului elimină candidații
#    ÎNAINTE de orice ranking pe cost/calitate.
# 2. Candidat cu status != "available" nu e eligibil.
# 3. require_evaluated=True + quality_score=None → candidat eliminat.
# 4. Niciun candidat eligibil → REFUSE cu codul specific; best_effort poate fi
#    setat, dar decision rămâne REFUSE și requires_escalation=True.
# 5. ROUTE impune met_bar=True — nu există „ales cu rezerve".
# 6. Orice decizie poartă policy_ref + correlation_id pentru audit/telemetrie.
# 7. Routerul NU apelează provideri, NU măsoară latențe live și NU decide
#    fallback automat — acestea rămân la politica de execuție a produsului.
