"""Webhook-level tests for the Telegram bot: pairing codes and the roster gate.

The Executive itself is never invoked — ``_process_and_reply`` and
``send_message`` are replaced with mocks so the tests cover only the
routing decisions the webhook makes before any model call.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.integrations import telegram_bot
from openexecutive.people import store as people_store

TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDEFGH"
SECRET = "webhook-s3cret"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[Any, ...]]]:
    """Fresh people DB, a webhook secret, mocked outbound + Executive, no audit rows on disk."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_store.initialize_db()
    audited: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: audited.append((event_type, summary, kw.get("details") or {})),
    )
    monkeypatch.setattr(telegram_bot, "send_message", AsyncMock(return_value="1"))
    monkeypatch.setattr(telegram_bot, "_process_and_reply", AsyncMock(return_value=None))
    telegram_bot._unsolicited_reply_at.clear()
    yield audited
    telegram_bot._unsolicited_reply_at.clear()


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(telegram_bot.router)
    return TestClient(app, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})


def _update(
    text: str | None, chat_id: int = 8519677317, message_id: int = 1, chat_type: str | None = None
) -> dict[str, Any]:
    return {
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": chat_type or ("private" if chat_id > 0 else "group")},
            "from": {"first_name": "John", "last_name": "Rufus"},
            "text": text,
        }
    }


def _sent() -> list[str]:
    return [call.args[2] for call in telegram_bot.send_message.await_args_list]  # type: ignore[attr-defined]


def _outcomes(audited: list[tuple[Any, ...]]) -> list[str]:
    return [d.get("outcome") for _, _, d in audited if d.get("outcome")]


# --------------------------------------------------------------------------- #
# Pairing
# --------------------------------------------------------------------------- #

def test_start_with_live_code_links_chat(client: TestClient, _isolate: list[tuple[Any, ...]]) -> None:
    pid = people_store.upsert_person(full_name="John Rufus", email="john@example.com")
    code, _ = people_store.create_telegram_link_code(pid)

    resp = client.post("/webhook/telegram", json=_update(f"/start {code}"))
    assert resp.status_code == 200

    person = people_store.get_person(pid)
    assert person is not None and person.telegram_chat_id == "8519677317"
    assert _sent() == [telegram_bot.LINKED_REPLY.format(name="John Rufus")]
    assert _outcomes(_isolate) == ["telegram_linked"]
    telegram_bot._process_and_reply.assert_not_awaited()  # type: ignore[attr-defined]


def test_start_with_bot_suffix_and_dead_code(client: TestClient, _isolate: list[tuple[Any, ...]]) -> None:
    resp = client.post("/webhook/telegram", json=_update("/start@exec_bot AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
    assert resp.status_code == 200
    assert _sent() == [telegram_bot.LINK_EXPIRED_REPLY]
    assert _outcomes(_isolate) == ["telegram_link_rejected"]
    telegram_bot._process_and_reply.assert_not_awaited()  # type: ignore[attr-defined]

    # A second guess inside the cooldown gets no feedback at all.
    client.post("/webhook/telegram", json=_update("/start BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"))
    assert len(_sent()) == 1


@pytest.mark.parametrize("chat_id, chat_type", [(-1001234567890, "supergroup"), (8519677317, "group")])
def test_code_from_non_private_chat_is_never_consumed(
    client: TestClient, _isolate: list[tuple[Any, ...]], chat_id: int, chat_type: str
) -> None:
    """Both the id sign and Telegram's chat.type must say private."""
    pid = people_store.upsert_person(full_name="John Rufus")
    code, _ = people_store.create_telegram_link_code(pid)
    resp = client.post("/webhook/telegram", json=_update(f"/start {code}", chat_id=chat_id, chat_type=chat_type))
    assert resp.status_code == 200
    assert people_store.get_person(pid).telegram_chat_id is None  # type: ignore[union-attr]
    assert people_store.consume_telegram_link_code(code, "5") is not None  # still live
    assert _sent() == []  # non-private chats stay silent
    assert _outcomes(_isolate) == ["rejected_unknown_sender"]


def test_expired_reply_and_pointer_have_separate_cooldowns(
    client: TestClient, _isolate: list[tuple[Any, ...]]
) -> None:
    """The stranger who said "hi" and then taps a dead link must still hear it failed."""
    client.post("/webhook/telegram", json=_update("hi"))
    client.post("/webhook/telegram", json=_update("/start AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
    assert _sent() == [
        telegram_bot.UNKNOWN_SENDER_REPLY.format(chat_id=8519677317),
        telegram_bot.LINK_EXPIRED_REPLY,
    ]


def test_no_unsolicited_replies_without_webhook_secret(
    client: TestClient, _isolate: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unverified updates could be forged, so they can't make the bot message anyone."""
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    client.post("/webhook/telegram", json=_update("hi"))
    client.post("/webhook/telegram", json=_update("/start AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
    assert _sent() == []
    assert [d["replied"] for _, _, d in _isolate] == [False, False]
    # A valid code still links (it is its own proof) and confirms.
    pid = people_store.upsert_person(full_name="John Rufus")
    code, _ = people_store.create_telegram_link_code(pid)
    client.post("/webhook/telegram", json=_update(f"/start {code}"))
    assert _sent() == [telegram_bot.LINKED_REPLY.format(name="John Rufus")]


def test_wrong_secret_is_refused(client: TestClient) -> None:
    resp = client.post("/webhook/telegram", json=_update("hi"), headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert resp.status_code == 401


def test_null_caption_from_stranger_does_not_500(client: TestClient, _isolate: list[tuple[Any, ...]]) -> None:
    payload = _update(None)
    payload["message"]["caption"] = None
    resp = client.post("/webhook/telegram", json=payload)
    assert resp.status_code == 200
    assert _outcomes(_isolate) == ["rejected_unknown_sender"]


# --------------------------------------------------------------------------- #
# Roster gate
# --------------------------------------------------------------------------- #

def test_unknown_private_sender_gets_one_pointer_per_cooldown(
    client: TestClient, _isolate: list[tuple[Any, ...]]
) -> None:
    client.post("/webhook/telegram", json=_update("hello?"))
    client.post("/webhook/telegram", json=_update("/start"))  # Telegram's Start button
    client.post("/webhook/telegram", json=_update("anyone there?"))

    sent = _sent()
    assert sent == [telegram_bot.UNKNOWN_SENDER_REPLY.format(chat_id=8519677317)]
    assert "8519677317" in sent[0]
    assert _outcomes(_isolate) == ["rejected_unknown_sender"] * 3
    assert [d["replied"] for _, _, d in _isolate] == [True, False, False]
    telegram_bot._process_and_reply.assert_not_awaited()  # type: ignore[attr-defined]


def test_unknown_group_sender_stays_silent(client: TestClient, _isolate: list[tuple[Any, ...]]) -> None:
    client.post("/webhook/telegram", json=_update("hello", chat_id=-100123))
    assert _sent() == []
    assert [d["replied"] for _, _, d in _isolate] == [False]


def test_rostered_sender_reaches_executive_with_command_stripped(client: TestClient) -> None:
    people_store.upsert_person(full_name="John Rufus", telegram_chat_id="8519677317")
    resp = client.post("/webhook/telegram", json=_update("/start what's on today?"))
    assert resp.status_code == 200
    telegram_bot._process_and_reply.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = telegram_bot._process_and_reply.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["message_text"] == "what's on today?"
    assert kwargs["chat_id"] == 8519677317 and kwargs["token"] == TOKEN
    assert _sent() == []


def test_rostered_sender_start_with_words_is_not_a_pairing_attempt(client: TestClient) -> None:
    """Only an exact 32-char payload is a code; other /start text reaches the Executive."""
    people_store.upsert_person(full_name="John Rufus", telegram_chat_id="8519677317")
    client.post("/webhook/telegram", json=_update("/start help_me_with_pricing_please"))
    telegram_bot._process_and_reply.assert_awaited_once()  # type: ignore[attr-defined]
    assert telegram_bot._process_and_reply.await_args.kwargs["message_text"] == "help_me_with_pricing_please"  # type: ignore[attr-defined]
    assert _sent() == []


def test_redelivered_link_after_success_stays_quiet(client: TestClient, _isolate: list[tuple[Any, ...]]) -> None:
    pid = people_store.upsert_person(full_name="John Rufus")
    code, _ = people_store.create_telegram_link_code(pid)
    client.post("/webhook/telegram", json=_update(f"/start {code}"))
    client.post("/webhook/telegram", json=_update(f"/start {code}"))  # Telegram retry of the same update
    assert _sent() == [telegram_bot.LINKED_REPLY.format(name="John Rufus")]
    assert _outcomes(_isolate) == ["telegram_linked", "telegram_link_rejected"]
    assert _isolate[1][2]["already_linked"] is True and _isolate[1][2]["replied"] is False


def test_relink_audits_displaced_person(client: TestClient, _isolate: list[tuple[Any, ...]]) -> None:
    old = people_store.upsert_person(full_name="Old", telegram_chat_id="8519677317")
    new = people_store.upsert_person(full_name="New")
    code, _ = people_store.create_telegram_link_code(new)
    client.post("/webhook/telegram", json=_update(f"/start {code}"))
    assert _isolate[0][2]["displaced_person_ids"] == [old]
    assert people_store.get_person(old).telegram_chat_id is None  # type: ignore[union-attr]


def test_non_integer_chat_id_is_dropped_not_500(client: TestClient, _isolate: list[tuple[Any, ...]]) -> None:
    payload = _update("hi")
    payload["message"]["chat"]["id"] = "8519677317"
    assert client.post("/webhook/telegram", json=payload).status_code == 200
    assert _outcomes(_isolate) == [] and _sent() == []


def test_rostered_sender_bare_start_is_ignored(client: TestClient) -> None:
    people_store.upsert_person(full_name="John Rufus", telegram_chat_id="8519677317")
    client.post("/webhook/telegram", json=_update("/start"))
    telegram_bot._process_and_reply.assert_not_awaited()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def test_unsolicited_reply_cooldown_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram_bot, "_REPLY_COOLDOWN_MAX_TRACKED", 2)
    may = telegram_bot._may_send_unsolicited
    assert may("rejected_unknown_sender", 1, now=0.0)
    assert may("rejected_unknown_sender", 2, now=0.0)
    # Full of live entries: a third stranger is not tracked and not notified.
    assert not may("rejected_unknown_sender", 3, now=1.0)
    # Same chat, same kind, inside the window: quiet.
    assert not may("rejected_unknown_sender", 1, now=1.0)
    # Once the entries age out they're pruned and a new chat gets its pointer.
    later = telegram_bot._REPLY_COOLDOWN_S + 1.0
    assert may("rejected_unknown_sender", 3, now=later)
    assert may("rejected_unknown_sender", 1, now=later + 1.0)


def test_deep_link_shape() -> None:
    assert telegram_bot.telegram_deep_link("exec_bot", "abc") == "https://t.me/exec_bot?start=abc"


@pytest.fixture()
def _fresh_username_cache() -> Iterator[None]:
    telegram_bot._bot_username_cache.clear()
    yield
    telegram_bot._bot_username_cache.clear()


@pytest.mark.asyncio
async def test_get_bot_username_caches_per_token(
    monkeypatch: pytest.MonkeyPatch, _fresh_username_cache: None
) -> None:
    import httpx

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        # The token is in the path; answer per bot so per-token keying is observable.
        name = "exec_bot" if TOKEN in request.url.path else "other_bot"
        return httpx.Response(200, json={"ok": True, "result": {"username": name}})

    monkeypatch.setattr(telegram_bot, "_http_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    other = "654321:ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvuts"
    assert await telegram_bot.get_bot_username(TOKEN) == "exec_bot"
    assert await telegram_bot.get_bot_username(TOKEN) == "exec_bot"
    assert await telegram_bot.get_bot_username(other) == "other_bot"
    assert await telegram_bot.get_bot_username(TOKEN) == "exec_bot"
    assert len(calls) == 2 and all(c.endswith("/getMe") for c in calls)


@pytest.mark.asyncio
async def test_get_bot_username_failure_is_cached_briefly_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, _fresh_username_cache: None
) -> None:
    import httpx

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr(telegram_bot, "_http_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await telegram_bot.get_bot_username(TOKEN) is None
    assert await telegram_bot.get_bot_username(TOKEN) is None  # negative cache: no second call
    assert calls == 1
