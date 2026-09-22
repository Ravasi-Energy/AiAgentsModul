"""Registry of the five BOAgents settings shipped in Valul 1 (BO-SET-001).

Each entry is a typed contract, not just a name: it carries labels/help in
Romanian (the new UI surfaces are Romanian), a validator with explicit bounds,
an apply mode (IMMEDIATE | NEW_RUN — the only two this slice uses), the role
required to edit, and an audit redaction class. ``sensitivity="normal"`` for
all five — none is a secret, so values may appear in audit details.

The 420-key catalog in the coordination package is backlog; only keys
registered here exist at runtime, and the UI states exactly that.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SettingType = Literal["text", "enum", "integer", "timezone"]
ApplyMode = Literal["IMMEDIATE", "NEW_RUN", "RESTART", "MIGRATION"]
Scope = Literal["tenant"]

_SCHEMA_VERSION = "bo.settings.v1"

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class SettingValidationError(ValueError):
    """Raised when a candidate value fails the key's validation rules."""


def _validate_text(v: Any, *, min_len: int, max_len: int, label: str) -> str:
    if not isinstance(v, str):
        raise SettingValidationError(f"{label}: așteptat text")
    v = v.strip()
    if _CONTROL_CHAR_RE.search(v):
        raise SettingValidationError(f"{label}: caractere de control interzise")
    if not (min_len <= len(v) <= max_len):
        raise SettingValidationError(
            f"{label}: lungimea trebuie să fie între {min_len} și {max_len}"
        )
    return v


def _validate_enum(v: Any, *, allowed: tuple[str, ...], label: str) -> str:
    if not isinstance(v, str) or v not in allowed:
        raise SettingValidationError(
            f"{label}: valoare permisă: {', '.join(allowed)}"
        )
    return v


def _validate_int(v: Any, *, minimum: int, maximum: int, label: str) -> int:
    # bool is a subclass of int — reject it explicitly so `true` isn't 1.
    if isinstance(v, bool) or not isinstance(v, int):
        raise SettingValidationError(f"{label}: așteptat număr întreg")
    if not (minimum <= v <= maximum):
        raise SettingValidationError(
            f"{label}: valoarea trebuie să fie între {minimum} și {maximum}"
        )
    return v


def _validate_timezone(v: Any, *, label: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise SettingValidationError(f"{label}: așteptat un fus IANA")
    v = v.strip()
    if len(v) > 64 or _CONTROL_CHAR_RE.search(v):
        raise SettingValidationError(f"{label}: nume de fus invalid")
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise SettingValidationError(
            f"{label}: fus necunoscut (folosește un nume IANA, ex. Europe/Bucharest)"
        ) from exc
    return v


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    type: SettingType
    default: Any
    apply_mode: ApplyMode
    scope: Scope
    page: str
    tab: str
    label_ro: str
    label_en: str
    help_ro: str
    owner_role: str
    edit_role: str
    sensitivity: str
    effect_ro: str
    acceptance_ro: str
    validate: Callable[[Any], Any] = field(compare=False)

    def to_meta(self) -> dict[str, Any]:
        """Public metadata for the settings UI — never includes the value."""
        return {
            "key": self.key,
            "schema_version": _SCHEMA_VERSION,
            "type": self.type,
            "default": self.default,
            "apply_mode": self.apply_mode,
            "scope": self.scope,
            "page": self.page,
            "tab": self.tab,
            "label_ro": self.label_ro,
            "label_en": self.label_en,
            "help_ro": self.help_ro,
            "edit_role": self.edit_role,
            "sensitivity": self.sensitivity,
            "effect_ro": self.effect_ro,
        }


REGISTRY: dict[str, SettingSpec] = {
    "bo.ui.display_name": SettingSpec(
        key="bo.ui.display_name",
        type="text",
        default="BOAgents",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Numele afișat",
        label_en="Display name",
        help_ro="Numele instalației afișat în interfață.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică imediat în interfață după salvare.",
        acceptance_ro="Salvare, reîncărcare și restart păstrează valoarea.",
        validate=lambda v: _validate_text(v, min_len=1, max_len=80, label="Numele afișat"),
    ),
    "bo.ui.language": SettingSpec(
        key="bo.ui.language",
        type="enum",
        default="ro",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Limba interfeței",
        label_en="UI language",
        help_ro="Limba suprafețelor noi ale produsului.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică imediat la suprafețele care respectă setarea.",
        acceptance_ro="Doar ro/en sunt acceptate; alte valori sunt respinse.",
        validate=lambda v: _validate_enum(v, allowed=("ro", "en"), label="Limba interfeței"),
    ),
    "bo.ui.timezone": SettingSpec(
        key="bo.ui.timezone",
        type="timezone",
        default="Europe/Bucharest",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Fus orar",
        label_en="Timezone",
        help_ro="Fus IANA folosit la afișarea orei în suprafețele noi.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică imediat la timestampurile afișate în paginile BO.",
        acceptance_ro="Fusurile invalide sunt respinse cu mesaj explicit.",
        validate=lambda v: _validate_timezone(v, label="Fus orar"),
    ),
    "bo.bobot.simulation.max_steps": SettingSpec(
        key="bo.bobot.simulation.max_steps",
        type="integer",
        default=50,
        apply_mode="NEW_RUN",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Limită de pași per simulare",
        label_en="Simulation step limit",
        help_ro="Numărul maxim de pași evaluați într-o simulare BoBot.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică la simulările pornite după salvare; nu modifică rulările existente.",
        acceptance_ro="O simulare nouă respectă limita; valori în afara 1–500 sunt respinse.",
        validate=lambda v: _validate_int(v, minimum=1, maximum=500, label="Limita de pași"),
    ),
    "bo.bobot.simulation.retention_days": SettingSpec(
        key="bo.bobot.simulation.retention_days",
        type="integer",
        default=30,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Retenție istoric simulări (zile)",
        label_en="Simulation history retention (days)",
        help_ro="Cât se păstrează rulările de simulare. Nu atinge auditul.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="La următoarea curățare se șterg doar rulările de simulare mai vechi decât pragul.",
        acceptance_ro="Sweep-ul șterge doar rulări de simulare expirate; auditul rămâne neatins.",
        validate=lambda v: _validate_int(v, minimum=1, maximum=3650, label="Retenția"),
    ),
}


def validate_value(key: str, value: Any) -> Any:
    spec = REGISTRY.get(key)
    if spec is None:
        raise SettingValidationError(f"Setare necunoscută: {key}")
    return spec.validate(value)
