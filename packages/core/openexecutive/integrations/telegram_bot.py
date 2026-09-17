from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from openexecutive.config import get_settings
from openexecutive.people.store import TELEGRAM_LINK_TTL

logger = logging.getLogger(__name__)
router = APIRouter()

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
# Telegram's 4096 limit is in UTF-16 code units; emoji count double.
# Use 2000 chars as a conservative safe limit.
_MAX_MSG_LEN = 2000

# Module-level client — reused across requests to avoid per-call TLS handshakes.
_http_client: httpx.AsyncClient | None = None

# Per-chat lock so two messages from the same chat serialize — without this,
# concurrent handlers both load stale history and write interleaved turns.
_chat_locks: dict[int, asyncio.Lock] = {}


def _chat_lock(chat_id: int) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30)
    return _http_client


def _tg_url(token: str, method: str) -> str:
    return _TELEGRAM_API.format(token=token, method=method)


# --------------------------------------------------------------------------- #
# Self-serve chat linking
#
# A Person taps https://t.me/<bot>?start=<code>; Telegram delivers
# "/start <code>" from their private chat and the webhook binds that chat id
# to the Person (people.store.consume_telegram_link_code) before the roster
# gate. Unrecognised private senders get a short pointer instead of silence —
# but only on updates the webhook secret has verified (see telegram_webhook).
# --------------------------------------------------------------------------- #

# Telegram's deep-link payload charset is [A-Za-z0-9_-], 1..64 chars; our codes
# are always exactly 32 (secrets.token_urlsafe(24)). Matching the exact length
# keeps a rostered person's "/start <some words>" from being mistaken for a
# pairing attempt and swallowed.
_START_CODE_RE = re.compile(r"^/start(?:@\w+)?\s+([A-Za-z0-9_\-]{32})$", re.IGNORECASE)
_LINK_TTL_MINUTES = int(TELEGRAM_LINK_TTL.total_seconds() // 60)

LINKED_REPLY = "Linked — you're now talking to the Executive as {name}. Send a message to start."
# Deliberately generic: a stranger learns nothing about the product or how
# access is granted beyond "ask the operator".
LINK_EXPIRED_REPLY = (
    f"That link has expired or was already used. Ask for a new one and tap it within {_LINK_TTL_MINUTES} minutes."
)
UNKNOWN_SENDER_REPLY = (
    "This bot isn't available to your Telegram account yet. Your chat ID is {chat_id} — "
    "give it to whoever runs the bot, or ask them for a link to tap."
)

# One unsolicited reply per (kind, chat) per cooldown, so a flood of strangers
# or someone guessing codes costs at most one sendMessage per chat per window
# and gets no per-attempt feedback. Kinds are tracked separately so the
# pointer a stranger got for saying "hi" doesn't swallow the "link expired"
# reply they need a minute later. Bounded so fresh chat ids can't grow it.
_REPLY_COOLDOWN_S = 600
_REPLY_COOLDOWN_MAX_TRACKED = 10_000
_unsolicited_reply_at: dict[tuple[str, int], float] = {}


def _may_send_unsolicited(kind: str, chat_id: int, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    key = (kind, chat_id)
    last = _unsolicited_reply_at.get(key)
    if last is not None and now - last < _REPLY_COOLDOWN_S:
        return False
    if len(_unsolicited_reply_at) >= _REPLY_COOLDOWN_MAX_TRACKED:
        stale = [k for k, ts in _unsolicited_reply_at.items() if now - ts >= _REPLY_COOLDOWN_S]
        for k in stale:
            _unsolicited_reply_at.pop(k, None)
        if len(_unsolicited_reply_at) >= _REPLY_COOLDOWN_MAX_TRACKED:
            # Still full of live entries: stay quiet rather than grow.
            return False
    _unsolicited_reply_at[key] = now
    return True


# getMe answers are stable; cache per token so minting a link doesn't hit
# Telegram every time. Single-flight behind a lock, a short timeout, and a
# brief negative cache keep a Telegram outage from stacking up slow mints.
_BOT_USERNAME_TTL_S = 3600
_BOT_USERNAME_NEGATIVE_TTL_S = 60
_BOT_USERNAME_TIMEOUT_S = 10
_BOT_USERNAME_MAX_CACHED = 8  # one token per instance in practice
_bot_username_cache: dict[str, tuple[str | None, float]] = {}
_bot_username_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _username_lock() -> asyncio.Lock:
    """A lock bound to the running loop (tests spin up a loop per test)."""
    global _bot_username_lock
    loop = asyncio.get_running_loop()
    if _bot_username_lock is None or _bot_username_lock[0] is not loop:
        _bot_username_lock = (loop, asyncio.Lock())
    return _bot_username_lock[1]


async def get_bot_username(token: str) -> str | None:
    """The bot's @username (without the @), or None if Telegram can't tell us."""
    async with _username_lock():
        now = time.monotonic()
        cached = _bot_username_cache.get(token)
        if cached is not None and cached[1] > now:
            return cached[0]
        username: str | None = None
        try:
            resp = await _get_http_client().get(_tg_url(token, "getMe"), timeout=_BOT_USERNAME_TIMEOUT_S)
            if resp.is_success:
                value = resp.json().get("result", {}).get("username")
                username = str(value) if value else None
            else:
                logger.warning("Telegram getMe failed: %s", resp.status_code)
        except Exception as exc:
            # Only the type: httpx's repr carries the URL, which embeds the token.
            logger.warning("Telegram getMe error: %s", type(exc).__name__)
        if len(_bot_username_cache) >= _BOT_USERNAME_MAX_CACHED:
            _bot_username_cache.clear()
        ttl = _BOT_USERNAME_TTL_S if username else _BOT_USERNAME_NEGATIVE_TTL_S
        _bot_username_cache[token] = (username, now + ttl)
        return username


def telegram_deep_link(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={code}"


async def _audit_inbound(summary: str, details: dict[str, Any]) -> None:
    """Audit row, written off the event loop (sqlite)."""
    from openexecutive.audit import log_event as audit_log

    await run_in_threadpool(
        audit_log, "integration_inbound", summary, actor="telegram", details={"channel": "telegram", **details}
    )


async def _reject_inbound(
    background_tasks: BackgroundTasks,
    *,
    token: str,
    chat_id: int,
    outcome: str,
    summary: str,
    reply: str,
    may_reply: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    """Audit a dropped update and, when allowed, send one pointer per cooldown."""
    replied = may_reply and _may_send_unsolicited(outcome, chat_id)
    logger.warning("Telegram: rejected %s (replied=%s)", summary, replied)
    await _audit_inbound(
        f"Rejected: {summary}", {"chat_id": chat_id, "outcome": outcome, "replied": replied, **(extra or {})}
    )
    if replied:
        background_tasks.add_task(send_message, token, chat_id, reply)


async def _try_pairing(
    background_tasks: BackgroundTasks, *, token: str, chat_id: int, text: str, may_reply: bool
) -> bool:
    """Consume a ``/start <code>`` pairing link. True when the update was handled
    (linked or rejected) and must not reach the Executive."""
    match = _START_CODE_RE.match(text)
    if not match:
        return False
    from openexecutive.people.store import (
        consume_telegram_link_code,
        find_person_by_telegram_chat_id,
    )

    result = await run_in_threadpool(consume_telegram_link_code, match.group(1), str(chat_id))
    if result is None:
        # A chat that is already linked and presents a dead code is almost
        # always Telegram re-delivering the update that linked it: stay quiet
        # rather than tell a freshly linked person their link expired.
        already = await run_in_threadpool(find_person_by_telegram_chat_id, str(chat_id))
        await _reject_inbound(
            background_tasks,
            token=token,
            chat_id=chat_id,
            outcome="telegram_link_rejected",
            summary=f"telegram link code from chat_id={chat_id} not live",
            reply=LINK_EXPIRED_REPLY,
            may_reply=may_reply and already is None,
            extra={"already_linked": already is not None},
        )
        return True
    from openexecutive.people import registry as people_registry

    people_registry.invalidate()
    person = result.person
    logger.info(
        "Telegram: linked chat_id=%s to person_id=%s (displaced=%s)", chat_id, person.id, result.displaced_person_ids
    )
    await _audit_inbound(
        f"Linked telegram chat_id={chat_id} to {person.full_name}",
        {
            "chat_id": chat_id,
            "person_id": person.id,
            # Anyone who held this chat id before and lost it — an access
            # removal, so it must be on the record.
            "displaced_person_ids": result.displaced_person_ids,
            "outcome": "telegram_linked",
        },
    )
    background_tasks.add_task(send_message, token, chat_id, LINKED_REPLY.format(name=person.full_name))
    return True


def _split_message(text: str) -> list[str]:
    """Split a long response into ≤_MAX_MSG_LEN-char chunks on paragraph boundaries."""
    if len(text) <= _MAX_MSG_LEN:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= _MAX_MSG_LEN:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, _MAX_MSG_LEN)
        if split_at <= 0:
            split_at = text.rfind("\n", 0, _MAX_MSG_LEN)
        if split_at <= 0:
            split_at = _MAX_MSG_LEN
        chunk = text[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remainder = text[split_at:].strip()
        if remainder == text:
            # No progress — force a hard split to avoid infinite loop.
            chunks.append(text[:_MAX_MSG_LEN])
            text = text[_MAX_MSG_LEN:]
        else:
            text = remainder
    return [c for c in chunks if c]


async def send_message(token: str, chat_id: int, text: str) -> str | None:
    """Send one or more messages to a Telegram chat, splitting if needed.

    Returns the Telegram message_id of the last chunk sent (best-effort —
    ``None`` if no chunk delivered or the response lacks one), so callers can
    link an outbound DM to a later reply. Existing callers ignore the return."""
    client = _get_http_client()
    last_message_id: str | None = None
    for chunk in _split_message(text):
        if not chunk:
            continue
        resp = await client.post(
            _tg_url(token, "sendMessage"),
            json={"chat_id": chat_id, "text": chunk},
        )
        if resp.is_error:
            logger.error(
                "Telegram sendMessage failed: %s %s", resp.status_code, resp.text
            )
            continue
        try:
            mid = resp.json().get("result", {}).get("message_id")
            if mid is not None:
                last_message_id = str(mid)
        except (ValueError, TypeError, AttributeError):
            pass
    return last_message_id


async def _get_telegram_file_bytes(token: str, file_id: str) -> tuple[str, bytes]:
    """Resolve a Telegram file_id to a download URL and fetch the bytes.

    Returns ``(file_path_on_tg_servers, data)``.  Raises on any error so the
    caller can skip the attachment and log it.
    """
    from openexecutive.integrations.attachments import download_bytes

    client = _get_http_client()
    resp = await client.get(_tg_url(token, f"getFile?file_id={file_id}"))
    resp.raise_for_status()
    result = resp.json().get("result", {})
    file_path = result.get("file_path", "")
    if not file_path:
        raise ValueError(f"Telegram getFile returned no file_path for file_id={file_id}")

    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    data = await download_bytes(url)
    return file_path, data


async def _process_and_reply(
    message_text: str,
    sender_name: str,
    chat_id: int,
    message_id: int,
    token: str,
    attachment_file_ids: list[tuple[str, str, str]] | None = None,
) -> None:
    """Handle one inbound Telegram message.

    ``attachment_file_ids`` is a list of ``(file_id, filename, content_type)``
    tuples for any files or photos attached to the message.
    """
    # Deterministic per-chat session id — stamped on every audit row (inbound,
    # chat_turn, specialist_consult, tool_invocation) so a single request can
    # be followed end-to-end in /audit. Must match the value used when the
    # Session is constructed below.
    session_id = f"telegram:{chat_id}"

    from openexecutive.audit import log_event as audit_log
    audit_log(
        "integration_inbound",
        f"Inbound telegram from {sender_name} (chat_id={chat_id}): {message_text[:160]}",
        actor="telegram",
        session_id=session_id,
        details={
            "channel": "telegram",
            "chat_id": chat_id,
            "message_id": message_id,
            "sender": sender_name,
            "text_len": len(message_text),
        },
    )
    # WaitForHuman inbound resolver — check BEFORE alert triage.
    try:
        from openexecutive.people.store import find_person_by_telegram_chat_id
        from openexecutive.workflows.inbound_resolver import resolve_inbound_message
        from openexecutive.workflows.resumer import apply_resolution

        person = find_person_by_telegram_chat_id(str(chat_id))
        if person is not None and person.id is not None:
            resolution = await resolve_inbound_message(
                channel="telegram",
                channel_ref=str(chat_id),
                from_person_id=person.id,
                text=message_text,
                message_id=str(message_id),
                in_reply_to="",
            )
            if resolution is not None and resolution.run_id:
                success = await apply_resolution(resolution.run_id, resolution)
                if success:
                    await send_message(token, chat_id, "Got it — your response has been recorded.")
                    return
    except Exception:
        logger.exception("Telegram: inbound resolver check failed")

    # Fork into alert triage pipeline (fire-and-forget, same pattern as other integrations).
    try:
        from openexecutive.alerts.models import AlertEvent
        from openexecutive.alerts.pipeline import schedule_evaluation

        schedule_evaluation(
            AlertEvent(
                source="telegram",
                external_id=str(message_id),
                body=message_text,
                user=sender_name,
            )
        )
    except Exception:
        logger.exception("Telegram: failed to schedule alert evaluation")

    # Process file / photo attachments before entering the chat lock so that
    # slow downloads don't hold the lock while the chat serialises.
    att_image_blocks: list[dict] = []
    if attachment_file_ids:
        try:
            from openexecutive.integrations.attachments import build_attachment_output

            for file_id, filename, content_type in attachment_file_ids:
                try:
                    _file_path, data = await _get_telegram_file_bytes(token, file_id)
                    # build_attachment_output works on bytes directly — we
                    # don't need AttachmentItem/process_attachments here since
                    # Telegram requires a separate getFile API call rather than
                    # a direct URL download.
                    att_text, img_blocks = build_attachment_output(filename, data, content_type)
                    if att_text:
                        message_text = (
                            f"{att_text}\n\n{message_text}" if message_text else att_text
                        )
                    att_image_blocks.extend(img_blocks)
                except Exception:
                    logger.exception(
                        "Telegram: failed to download/process attachment file_id=%s", file_id
                    )
        except Exception:
            logger.exception("Telegram: attachment processing setup failed")

    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.memory.session_store import (
        create_session,
        load_messages,
        save_message,
        update_session_timestamp,
    )
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.session import Session

    response: str | None = None
    async with _chat_lock(chat_id):
        try:
            profile = load_or_create_profile()
            session = Session(
                session_id=session_id,
                company_profile=profile if not profile.is_empty() else None,
            )
            history = load_messages(session_id)
            if history:
                session.conversation_history = history
            # Record this channel_ref as user-initiated so the Executive may
            # schedule follow-ups back to this chat.
            session.seen_channel_refs.add(("telegram", str(chat_id)))
            retrieved_context = retrieve(query=message_text)
            episodic_context = format_for_prompt(session_id=session_id)

            # Look up the OE Person record so Honcho can key per-person
            # memory off Person.id (shared across channels). No match →
            # person_id stays None and the Honcho layer no-ops.
            from openexecutive.people.store import find_person_by_telegram_chat_id
            person = find_person_by_telegram_chat_id(str(chat_id))
            person_id = person.id if person else None

            # Hydrate with the context of any recent outbound DM oe sent this
            # chat, so a reply oe solicited from another session lands with its
            # backstory. Injected into the model's copy only — message_text is
            # persisted to history below and must stay free of the one-shot
            # block. One-shot consumed inside the helper.
            #
            # Gated to 1:1 private chats (positive chat_id; Telegram groups and
            # channels are negative) so private outbound context can never be
            # pulled into a group — mirrors the Discord is_dm / Slack mode=="dm"
            # guards.
            chat_user_message = message_text
            if chat_id > 0:
                from openexecutive.integrations.inbound_hydration import (
                    hydrate_user_message,
                )

                chat_user_message = hydrate_user_message(
                    channel="telegram",
                    channel_ref=str(chat_id),
                    user_message=message_text,
                )

            response = await Executive(mcp_gateway=get_active_gateway()).chat(
                user_message=chat_user_message,
                session=session,
                retrieved_context=retrieved_context,
                episodic_context=episodic_context,
                attachment_blocks=att_image_blocks or None,
                person_id=person_id,
            )
            await send_message(token, chat_id, response)
        except Exception:
            logger.exception("Telegram: handler error for message %s", message_id)
            try:
                await send_message(
                    token,
                    chat_id,
                    "I encountered an error processing your request. Please try again.",
                )
            except Exception:
                logger.exception("Telegram: also failed to send error reply")
            return

        # Persist only after a successful reply. Failures here must not trigger
        # a user-facing error — the user already got their answer.
        #
        # Channel sessions are owned by the resolved sender if mapped to a
        # Person, otherwise fall back to the principal so unrostered channel
        # threads still appear in the operator's sidebar (instead of becoming
        # invisible NULL-owner rows).
        from openexecutive.people.store import find_principal_person
        session_owner_id = person_id
        if session_owner_id is None:
            principal = find_principal_person()
            session_owner_id = principal.id if principal is not None else None
        try:
            create_session(
                session_id,
                f"Telegram {sender_name}",
                session.created_at.isoformat(),
                caller_person_id=session_owner_id,
            )
            save_message(session_id, "user", message_text)
            save_message(session_id, "assistant", response)
            update_session_timestamp(session_id)
        except Exception:
            logger.exception(
                "Telegram: failed to persist turn for session %s", session_id
            )


# Known bot commands that should be stripped before passing to the Executive.
_COMMAND_RE = re.compile(r"^/(start|help|ask)(?:@\w+)?\s*", re.IGNORECASE)


@router.post("/webhook/telegram", status_code=200)
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    settings = get_settings()

    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Telegram integration not configured")

    # Verify the secret token Telegram sends in the header (set when registering the webhook).
    if settings.telegram_webhook_secret:
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(sent, settings.telegram_webhook_secret):
            logger.warning("Telegram: webhook secret mismatch")
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    # Parse body — return 200 on failure so Telegram doesn't retry bad payloads.
    try:
        body = await request.json()
    except Exception:
        logger.warning("Telegram: failed to parse JSON body")
        return {}

    # Only handle regular messages (ignore channel posts, edited messages, etc.)
    message = body.get("message")
    if not message:
        return {}

    # Extract fields safely — malformed payloads return 200 to stop Telegram retries.
    try:
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or not isinstance(message_id, int):
            raise TypeError("chat.id / message_id must be integers")
    except (KeyError, TypeError):
        logger.warning("Telegram: malformed message payload, missing chat.id or message_id")
        return {}

    from_user: dict[str, Any] = message.get("from") or {}
    sender_name = " ".join(
        filter(None, [from_user.get("first_name"), from_user.get("last_name")])
    ) or from_user.get("username") or f"chat:{chat_id}"

    from openexecutive.people.store import find_person_by_telegram_chat_id

    # Telegram may send either key with a null value.
    text = (message.get("text") or message.get("caption") or "").strip()
    # A 1:1 chat: positive id by convention AND Telegram's own chat.type, so
    # pairing (and pointers) can never target a group, where any member
    # could otherwise speak as the linked Person.
    chat_obj: dict[str, Any] = message.get("chat") or {}
    is_private = chat_id > 0 and chat_obj.get("type") == "private"
    # Unsolicited replies go only to updates the webhook secret has verified:
    # without one, anyone who knows the URL could make the bot message
    # arbitrary chats. Unverified deployments keep the legacy silent drop.
    may_reply = is_private and bool(settings.telegram_webhook_secret)
    token = settings.telegram_bot_token

    # Pairing runs BEFORE the roster gate — the whole point is that this chat
    # isn't on the roster yet. Never falls through to the Executive.
    if is_private and await _try_pairing(
        background_tasks, token=token, chat_id=chat_id, text=text, may_reply=may_reply
    ):
        return {}

    # Roster gate. The Telegram chat must match a non-archived Person with
    # telegram_chat_id set — via the /people UI or the pairing link above; the
    # old TELEGRAM_ALLOWED_CHAT_IDS env var has been removed.
    if await run_in_threadpool(find_person_by_telegram_chat_id, str(chat_id)) is None:
        await _reject_inbound(
            background_tasks,
            token=token,
            chat_id=chat_id,
            outcome="rejected_unknown_sender",
            summary=f"telegram chat_id={chat_id} not in People roster",
            reply=UNKNOWN_SENDER_REPLY.format(chat_id=chat_id),
            may_reply=may_reply,
        )
        return {}

    # Only strip known bot commands (/start, /help, /ask), not arbitrary slash-prefixed content.
    text = _COMMAND_RE.sub("", text).strip()

    # Collect attachment metadata (file_id, filename, content_type).
    # Downloads happen inside _process_and_reply so this handler stays fast.
    attachment_file_ids: list[tuple[str, str, str]] = []

    # Single document (any file type).
    doc = message.get("document")
    if doc:
        file_id = doc.get("file_id", "")
        filename = doc.get("file_name") or f"file_{file_id}"
        content_type = doc.get("mime_type") or ""
        if file_id:
            attachment_file_ids.append((file_id, filename, content_type))

    # Photos — Telegram sends an array of sizes; pick the largest.
    photos = message.get("photo")
    if photos and isinstance(photos, list) and photos:
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        file_id = largest.get("file_id", "")
        if file_id:
            attachment_file_ids.append((file_id, f"photo_{file_id}.jpg", "image/jpeg"))

    # Require either text or at least one attachment to proceed.
    if not text and not attachment_file_ids:
        return {}

    background_tasks.add_task(
        _process_and_reply,
        message_text=text,
        sender_name=sender_name,
        chat_id=chat_id,
        message_id=message_id,
        token=settings.telegram_bot_token,
        attachment_file_ids=attachment_file_ids or None,
    )
    return {}
