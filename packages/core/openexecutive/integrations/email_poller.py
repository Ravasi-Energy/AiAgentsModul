"""MCP-based inbound mailbox polling loop.

Polls the Executive's own mailbox through the configured workspace backend
(`integrations.workspace`: Gmail via workspace-mcp, or Outlook via
ms-365-mcp-server — `EMAIL_PROVIDER`). Each unread message is rendered into
one backend-neutral text (`workspace.mail.render_for_executive`) and handed
to the Executive, which decides what to do: reply, fetch attachments, create
an alert, or ignore. The `--- REPLY ---` block at the end of that text names
the exact reply tool and threading ids for the backend in use, so the persona
stays backend-neutral (and cacheable).

No reply logic, no attachment logic, no alert logic lives here — all of that is the
Executive's responsibility via its tool access. The backend argument shapes
live in `workspace/google.py` and `workspace/microsoft.py`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from openexecutive.config import get_settings
from openexecutive.integrations.workspace.google import _parse_recipients
from openexecutive.integrations.workspace.mail import (
    MailProvider,
    MessageRef,
    render_for_executive,
)
from openexecutive.integrations.workspace.registry import (
    get_mail_provider,
    provider_server_missing,
)

if TYPE_CHECKING:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = get_settings().email_poll_interval_seconds

# Prevents reprocessing the same message within a run (cleared on restart).
_processed_ids: set[str] = set()

_SKIP_SENDERS = ("noreply", "no-reply", "mailer-daemon", "postmaster", "do-not-reply")


def _mail_provider() -> MailProvider:
    """The configured mail backend, resolved through this module's `get_settings`
    (tests patch it with a stub; a stub without `email_provider` → google)."""
    return get_mail_provider(get_settings())


async def poll_once(gateway: MCPGateway, provider: MailProvider | None = None) -> None:
    """One poll cycle: find unread messages, hand each to the Executive."""
    settings = get_settings()
    user_email = settings.exec_email_address
    provider = provider or _mail_provider()

    refs = await provider.list_unread(gateway, user_email, 10)
    logger.debug("poll cycle — %d unread message(s)", len(refs))

    for ref in refs:
        mid = ref.message_id
        if not mid or mid in _processed_ids:
            continue
        try:
            await _handle_email(gateway, mid, ref.thread_id, user_email, provider=provider)
            _processed_ids.add(mid)
        except Exception:
            logger.exception("failed for message=%s", mid)


async def _handle_email(
    gateway: MCPGateway,
    message_id: str,
    thread_id: str,
    user_email: str,
    provider: MailProvider | None = None,
) -> None:
    provider = provider or _mail_provider()
    msg = await provider.fetch(gateway, MessageRef(message_id, thread_id), user_email)
    if msg is None:
        logger.warning("empty content for message=%s", message_id)
        return
    # The backend derived from_addr with parseaddr (or a structured field), so
    # adversarial From headers like `<a@evil.com> ignore previous instructions`
    # cannot smuggle trailing content into the [POLICY] notice below. Empty /
    # unparseable → from_addr stays empty; downstream guards (audit, roster
    # lookup, [POLICY] notice) handle that gracefully.
    from_addr = msg.from_addr
    thread_id = msg.thread_id or thread_id

    # Minimal guard: skip self-sent (prevents reply loops) and known automated senders.
    if from_addr.lower() == user_email.lower():
        logger.debug("skipping self-addressed message=%s", message_id)
        return
    sender_blob = f"{msg.from_name} {from_addr}".lower()
    if any(p in sender_blob for p in _SKIP_SENDERS):
        logger.debug("skipping automated sender for message=%s", message_id)
        return

    # Sender-roster awareness. Unrostered senders are NOT dropped — the
    # Executive still reads, classifies, and decides. What protects us
    # from auto-replying to spam is the outbound gate
    # (orchestrator.mcp_gateway: _check_gmail_recipients /
    # _check_m365_recipients), which refuses mail-send tool calls whose
    # recipient isn't on the People roster. The Executive sees a [POLICY]
    # notice prepended to the body (built in _run_executive) so it knows
    # reply tools will block and proposes to a human instead.
    from openexecutive.audit import log_event as audit_log
    from openexecutive.people.store import find_person_by_email
    sender_in_roster = find_person_by_email(from_addr) is not None
    if not sender_in_roster:
        logger.info(
            "non-roster sender=%s message=%s — routing to Executive (no auto-reply allowed)",
            from_addr, message_id,
        )
        audit_log(
            "integration_inbound",
            f"Accepted non-roster email from {from_addr} (reply blocked at outbound gate)",
            actor="email",
            details={
                "channel": "email",
                "from": from_addr,
                "message_id": message_id,
                "outcome": "accepted_non_roster",
            },
        )

    logger.info("routing message=%s to Executive", message_id)
    subject = msg.subject[:160]
    # Deterministic per-thread session id so every audit row from this inbound
    # (chat_turn, specialist_consult, tool_invocation) shares a grouping key
    # with the integration_inbound row. Falls back to from_addr when the
    # backend exposes no thread id.
    session_id = f"email:{thread_id or from_addr}"
    audit_log(
        "integration_inbound",
        f"Inbound email from {from_addr}: {subject}" if subject else f"Inbound email from {from_addr}",
        actor="email",
        session_id=session_id,
        details={
            "channel": "email",
            "provider": provider.name,
            "message_id": message_id,
            "thread_id": thread_id,
            "from": from_addr,
            "subject": subject,
        },
    )
    rendered = render_for_executive(msg, provider.reply_block(msg))
    try:
        await _run_executive(gateway, rendered, message_id, thread_id, from_addr, session_id)
    except Exception:
        logger.exception("Executive raised for message=%s", message_id)

    await _mark_read(gateway, message_id, user_email, provider=provider)


async def _run_executive(
    gateway: MCPGateway,
    raw_email: str,
    message_id: str,
    thread_id: str,
    from_addr: str = "",
    session_id: str | None = None,
) -> None:
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    profile = load_or_create_profile()
    session_kwargs: dict[str, Any] = {
        "company_profile": profile if not profile.is_empty() else None,
    }
    if session_id:
        session_kwargs["session_id"] = session_id
    session = Session(**session_kwargs)
    if from_addr:
        # Only register the sender as a schedulable channel_ref if they
        # are in the People roster. Without this guard, an attacker who
        # can spoof a From header could persuade the Executive (via
        # prompt injection in the body) to schedule outbound mail to
        # arbitrary third parties. The roster gate in _handle_email
        # already ensures we only get here for known senders, but
        # re-verify defensively — _run_executive is also reachable from
        # other code paths.
        from openexecutive.people.store import find_person_by_email

        settings = get_settings()
        if (
            from_addr.lower() == settings.exec_email_address.lower()
            or find_person_by_email(from_addr) is not None
        ):
            session.seen_channel_refs.add(("email", f"{from_addr}|{thread_id}"))
            session.seen_channel_refs.add(("email", from_addr))
    # Look up the OE Person record (case-insensitive by email) so Honcho
    # can key per-person memory off Person.id (shared across channels).
    # No match → person_id stays None and the Honcho layer no-ops.
    from openexecutive.people.store import find_person_by_email

    person_id: int | None = None
    if from_addr:
        person = find_person_by_email(from_addr)
        person_id = person.id if person else None

    # Multi-peer co-presence: parse To+Cc headers and resolve each
    # recipient to a Person via find_person_by_email. Skip the From
    # (already covered by person_id) and the OE exec's own address
    # (we ARE the executive — never a peer). Best-effort: parse
    # failures degrade to an empty list rather than blocking the turn.
    co_present_person_ids: list[int] = []
    try:
        recipients = _parse_recipients(raw_email)
        exec_email = get_settings().exec_email_address.lower()
        from_addr_lower = (from_addr or "").lower()
        for addr in recipients:
            addr_lower = addr.lower()
            if addr_lower in (exec_email, from_addr_lower):
                continue
            other = find_person_by_email(addr)
            if other and other.id is not None and other.id not in co_present_person_ids:
                co_present_person_ids.append(other.id)
    except Exception:
        logger.warning(
            "email: recipient parsing failed for message=%s — passing empty co-present list",
            message_id,
            exc_info=True,
        )

    # When the sender isn't on the People roster, prepend a [POLICY]
    # notice so the Executive doesn't waste a turn trying to auto-reply
    # (the MCP gateway's _check_gmail_recipients will block it anyway).
    # The notice lists the actions that ARE allowed so the model picks
    # the right path: classify, log, alert, or propose adding to roster.
    policy_notice = ""
    if from_addr and person_id is None:
        policy_notice = (
            f"[POLICY] This inbound is from {from_addr}, who is NOT on your team's "
            "People roster. You can classify it, log a decision, schedule an internal "
            "follow-up, alert the principal, or surface a proposal to add the sender "
            "to the roster. You cannot send an outbound reply directly to "
            f"{from_addr} — the email gateway will block it. To actually reply, the "
            "principal must add the sender to the People roster first.\n\n"
            "---\n\n"
        )

    # If this email is a reply to mail the Executive sent during another
    # session (e.g. web chat), hydrate the turn with that originating context
    # — the email analogue of the DM bots. channel_ref is the bare lowercased
    # sender address, matching what the gateway records at send time. No-op on
    # a miss, so a thread that already carries history is unaffected.
    base_message = (
        f"You have an inbound email (message_id={message_id}, thread_id={thread_id}).\n\n"
        f"{policy_notice}{raw_email}"
    )
    if from_addr:
        from openexecutive.integrations.inbound_hydration import (
            hydrate_user_message,
        )

        base_message = hydrate_user_message(
            channel="email",
            channel_ref=from_addr.lower(),
            user_message=base_message,
        )

    executive = Executive(mcp_gateway=gateway)
    # Default committee review on for inbound email. Emails tend to be
    # higher-stakes than ad-hoc chat (a recipient is going to read the
    # reply with no chance to interactively refine it), and the +5–12s
    # latency does not matter on a 60s poll cycle.
    await executive.chat(
        user_message=base_message,
        session=session,
        retrieved_context=retrieve(query=raw_email[:500]),
        episodic_context=format_for_prompt(),
        committee_review=True,
        person_id=person_id,
        co_present_person_ids=co_present_person_ids or None,
    )


async def _mark_read(
    gateway: MCPGateway, message_id: str, user_email: str, provider: MailProvider | None = None,
) -> None:
    provider = provider or _mail_provider()
    await provider.mark_read(gateway, MessageRef(message_id), user_email)


async def _discover_mail_tools(gateway: MCPGateway, provider: MailProvider) -> None:
    """Discover the backend's mail tools (extensible-mcp requires per-session discovery)."""
    for query in provider.discovery_queries:
        result = await gateway.search_tools({"query": query})
        logger.debug(
            "search_tools(%r) -> %r",
            query, str(result)[:200] if result else "",
        )
    logger.info("%s mail tools discovered", provider.name)


async def run_email_poller(gateway: MCPGateway, provider: MailProvider | None = None) -> None:
    """Async polling loop. Run as a background task; cancelled on shutdown.

    Fail-soft on a misconfigured switch: when the chosen backend's MCP server
    is not in the config, every cycle logs one ERROR and skips instead of
    spraying tool-not-found errors (and the API keeps serving).
    """
    settings = get_settings()
    provider = provider or get_mail_provider(settings)
    logger.info("started (provider=%s, interval=%ds)", provider.name, POLL_INTERVAL_SECONDS)
    config_path = getattr(settings, "mcp_servers_config_path", None)
    while True:
        try:
            if config_path is not None and provider_server_missing(provider.server_name, config_path):
                logger.error(
                    "email poller: EMAIL_PROVIDER=%s but MCP server '%s' is not defined in %s "
                    "— skipping this cycle",
                    provider.name, provider.server_name, config_path,
                )
            else:
                await _discover_mail_tools(gateway, provider)
                await poll_once(gateway, provider)
        except asyncio.CancelledError:
            logger.info("cancelled")
            raise
        except Exception:
            logger.exception("unexpected error in poll cycle")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
