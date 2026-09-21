"""BOAgents BoBot definition model — ``bo.bobot.v1``.

A BoBot is a *deterministic* automation: event → typed conditions → plan →
controls/approvals → authorized effect → receipt. Valul 1 ships the
definition/version/simulation slice only: runs are simulations and every
receipt is ``NOT_EXECUTED``. Nothing here calls an LLM, a provider, or the
network — the whole step grammar is closed-world (``extra="forbid"``).
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION: Literal["bo.bobot.v1"] = "bo.bobot.v1"

MAX_NAME = 120
MAX_DESCRIPTION = 500
MAX_MESSAGE = 500
MAX_STEPS = 64
MAX_REFS = 50
MAX_REF_LEN = 128
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EVENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$")

StepType = Literal["check", "emit_finding", "set_field", "notify", "note"]
TriggerType = Literal["manual", "event", "schedule"]
BotKind = Literal["BOT", "AI", "MIXED"]


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: TriggerType
    # event name for type="event" (e.g. "heartbeat.stale"); required there,
    # forbidden elsewhere so the grammar stays closed.
    event: str | None = None
    # schedule is declarative in Valul 1 — stored, never executed.
    cron: str | None = Field(default=None, max_length=120)

    @field_validator("event")
    @classmethod
    def _event_ok(cls, v: str | None, info) -> str | None:  # noqa: ANN001
        if info.data.get("type") == "event":
            if v is None or not _EVENT_RE.match(v):
                raise ValueError("trigger event cere un nume valid (ex. heartbeat.stale)")
        elif v is not None:
            raise ValueError("câmpul 'event' există numai pentru type=event")
        return v

    @field_validator("cron")
    @classmethod
    def _cron_ok(cls, v: str | None, info) -> str | None:  # noqa: ANN001
        if info.data.get("type") == "schedule":
            if v is None or not v.strip():
                raise ValueError("trigger schedule cere 'cron'")
        elif v is not None:
            raise ValueError("câmpul 'cron' există numai pentru type=schedule")
        return v


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: StepType
    # check: predicate gate over the run context.
    predicate: dict[str, Any] | None = None
    on_false: Literal["stop", "continue"] = "stop"
    # emit_finding
    finding_key: str | None = None
    category: str | None = Field(default=None, max_length=64)
    severity: Literal["info", "warning", "critical"] | None = None
    message: str | None = Field(default=None, max_length=MAX_MESSAGE)
    # set_field (mutates the *simulation* context only)
    path: str | None = Field(default=None, max_length=256)
    value: Any = None
    # notify
    channel: str | None = Field(default=None, max_length=64)

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"id de pas invalid: {v!r}")
        return v

    @field_validator("finding_key")
    @classmethod
    def _finding_key_ok(cls, v: str | None) -> str | None:
        if v is not None and not _ID_RE.match(v):
            raise ValueError(f"finding_key invalid: {v!r}")
        return v


class DefinitionContent(BaseModel):
    """The versioned payload — everything the hash covers."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["bo.bobot.v1"] = SCHEMA_VERSION
    trigger: Trigger
    predicates: dict[str, Any] | None = None
    steps: list[Step] = Field(default_factory=list, max_length=MAX_STEPS)
    capability_refs: list[str] = Field(default_factory=list, max_length=MAX_REFS)
    policy_refs: list[str] = Field(default_factory=list, max_length=MAX_REFS)

    @field_validator("capability_refs", "policy_refs")
    @classmethod
    def _refs_ok(cls, v: list[str]) -> list[str]:
        for ref in v:
            if not ref or len(ref) > MAX_REF_LEN:
                raise ValueError("referință vidă sau prea lungă")
        return v

    @field_validator("steps")
    @classmethod
    def _steps_ok(cls, v: list[Step]) -> list[Step]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("id-uri de pași duplicate")
        return v


def validate_step_semantics(steps: list[Step]) -> list[str]:
    """Per-type required-field checks that the model can't express alone."""
    errors: list[str] = []
    from openexecutive.bo.bots.predicates import PredicateError, _validate

    for step in steps:
        if step.type == "check":
            if step.predicate is None:
                errors.append(f"{step.id}: 'check' cere 'predicate'")
            else:
                try:
                    _validate(step.predicate, depth=1, budget=[64])
                except PredicateError as exc:
                    errors.append(f"{step.id}: predicat invalid: {exc}")
        elif step.type == "emit_finding":
            if not step.finding_key or not step.category or not step.severity or not step.message:
                errors.append(
                    f"{step.id}: 'emit_finding' cere finding_key, category, severity, message"
                )
        elif step.type == "set_field":
            if not step.path:
                errors.append(f"{step.id}: 'set_field' cere 'path'")
        elif step.type == "notify":
            if not step.message:
                errors.append(f"{step.id}: 'notify' cere 'message'")
            if step.channel != "internal":
                errors.append(
                    f"{step.id}: 'notify' permite numai channel=internal în Valul 1"
                )
        elif step.type == "note" and not step.message:
            errors.append(f"{step.id}: 'note' cere 'message'")
    return errors
