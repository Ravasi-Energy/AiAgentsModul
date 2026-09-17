"""FastAPI routes for People.

Phase 3 surface: CRUD + archive + approver lookup.
All mutations invalidate the 60s registry cache so the next
Executive turn picks up the change.
"""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from openexecutive.config import get_settings
from openexecutive.integrations import telegram_bot
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import (
    AuthorityScope,
    AvailabilityWindow,
    Person,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class PersonCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="", max_length=200)
    is_principal: bool = False
    department_slugs: list[str] = Field(default_factory=list)
    email: str | None = None
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    discord_user_id: str | None = None
    preferred_channel: str = "any"
    response_sla_hours: int = Field(default=24, ge=1, le=8760)
    on_leave_until: date | None = None
    reports_to_person_id: int | None = None
    authority_scope: list[AuthorityScope] = Field(default_factory=list)
    availability: list[AvailabilityWindow] = Field(default_factory=list)


class TelegramLinkCode(BaseModel):
    """A one-shot pairing code for a Person's Telegram chat.

    ``deep_link`` is ``https://t.me/<bot>?start=<code>`` when the bot's
    username is known; ``None`` when the instance has no Telegram token yet
    or Telegram couldn't be reached. The UI then falls back to a known bot
    username or shows the raw ``/start <code>`` instruction.
    """

    code: str
    deep_link: str | None
    bot_username: str | None
    expires_at: str


class PersonPatch(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    email: str | None = None
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    discord_user_id: str | None = None
    preferred_channel: str | None = None
    response_sla_hours: int | None = Field(default=None, ge=1, le=8760)
    on_leave_until: date | None = None
    clear_on_leave: bool = False
    reports_to_person_id: int | None = None
    department_slugs: list[str] | None = None
    authority_scope: list[AuthorityScope] | None = None
    availability: list[AvailabilityWindow] | None = None


# --------------------------------------------------------------------------- #
# Read routes
# --------------------------------------------------------------------------- #

@router.get("/people", response_model=list[Person])
def list_people(include_archived: bool = False) -> list[Person]:
    return people_store.list_people(include_archived=include_archived)


@router.get("/people/by-scope/{token}", response_model=list[Person])
def people_by_scope(token: str) -> list[Person]:
    """Return non-archived people who can approve the given scope token."""
    try:
        scope = AuthorityScope(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scope token: {token!r}. Valid tokens: {[s.value for s in AuthorityScope]}",
        ) from exc
    return people_store.find_approvers(scope)


@router.get("/people/{person_id}", response_model=Person)
def get_person(person_id: int) -> Person:
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


# --------------------------------------------------------------------------- #
# Mutation routes
# --------------------------------------------------------------------------- #

@router.post("/people", response_model=Person, status_code=status.HTTP_201_CREATED)
def create_person(body: PersonCreate) -> Person:
    pid = people_store.upsert_person(
        full_name=body.full_name,
        role=body.role,
        is_principal=body.is_principal,
        department_slugs=body.department_slugs,
        email=body.email,
        slack_user_id=body.slack_user_id,
        telegram_chat_id=body.telegram_chat_id,
        discord_user_id=body.discord_user_id,
        preferred_channel=body.preferred_channel,  # type: ignore[arg-type]
        response_sla_hours=body.response_sla_hours,
        on_leave_until=body.on_leave_until,
        reports_to_person_id=body.reports_to_person_id,
    )
    if body.authority_scope:
        people_store.set_authority_scope(pid, body.authority_scope)
    if body.availability:
        people_store.set_availability(pid, body.availability)
    people_registry.invalidate()
    person = people_store.get_person(pid)
    if person is None:
        raise HTTPException(status_code=500, detail="Person vanished after insert")
    return person


@router.patch("/people/{person_id}", response_model=Person)
def patch_person(person_id: int, body: PersonPatch) -> Person:
    if people_store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")

    raw = body.model_dump(exclude_unset=True)
    if raw:
        people_store.update_person(
            person_id,
            full_name=body.full_name,
            role=body.role,
            email=body.email,
            slack_user_id=body.slack_user_id,
            telegram_chat_id=body.telegram_chat_id,
            discord_user_id=body.discord_user_id,
            preferred_channel=body.preferred_channel,  # type: ignore[arg-type]
            response_sla_hours=body.response_sla_hours,
            on_leave_until=body.on_leave_until,
            clear_on_leave=body.clear_on_leave,
            reports_to_person_id=body.reports_to_person_id,
            department_slugs=body.department_slugs,
        )
    if "authority_scope" in raw:
        people_store.set_authority_scope(
            person_id, body.authority_scope or []
        )
    if "availability" in raw:
        people_store.set_availability(
            person_id, body.availability or []
        )
    people_registry.invalidate()
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=500, detail="Person vanished")
    return person


async def _mint_telegram_link(person_id: int) -> TelegramLinkCode:
    try:
        # sqlite write off the event loop (the route is async for getMe).
        code, expires_at = await run_in_threadpool(people_store.create_telegram_link_code, person_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Person not found") from exc
    token = get_settings().telegram_bot_token
    bot_username = await telegram_bot.get_bot_username(token) if token else None
    return TelegramLinkCode(
        code=code,
        deep_link=telegram_bot.telegram_deep_link(bot_username, code) if bot_username else None,
        bot_username=bot_username,
        expires_at=expires_at,
    )


# Declared before the ``{person_id}`` variant so "me" never reaches the int
# path parameter (422).
@router.post("/people/me/telegram-link", response_model=TelegramLinkCode)
async def create_my_telegram_link(request: Request) -> TelegramLinkCode:
    """A pairing code for the caller's own Person (their web identity).

    The caller is resolved the same way chat sessions are owned: the SaaS
    account's Person, else the ``x-caller-email`` header, else the principal.
    A signed-in caller with no Person row is refused rather than fused with
    the principal.
    """
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    person_id = _resolve_caller_person_id(request)
    if person_id is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "not_rostered", "message": "Your account isn't on the People roster yet"},
        )
    return await _mint_telegram_link(person_id)


@router.post("/people/{person_id}/telegram-link", response_model=TelegramLinkCode)
async def create_person_telegram_link(person_id: int) -> TelegramLinkCode:
    """A pairing code to hand to ``person_id``."""
    return await _mint_telegram_link(person_id)


@router.post("/people/{person_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_person(person_id: int) -> Response:
    if people_store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    people_store.archive_person(person_id)
    people_registry.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
