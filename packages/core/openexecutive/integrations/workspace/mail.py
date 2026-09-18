"""Mail backend contract + the one rendering every backend feeds the Executive.

Invariants every `MailProvider` MUST uphold:

1. `list_unread` / `fetch` / `mark_read` never raise on a transient or
   malformed backend response — return ``[]`` / ``None`` and log, so one bad
   poll cycle never kills the poller loop.
2. `fetch` derives `from_addr` with `email.utils.parseaddr` (or from a
   structured field), never by string-splitting a header: the address is
   interpolated into the `[POLICY]` notice the Executive reads.
3. `fetch` never surfaces a Reply-To-style redirect header. The Executive
   addresses replies itself; the gateway's roster gate is the enforcement,
   this keeps the attack surface out of the prompt entirely.
4. `build_send_arguments` produces the exact argument dict for
   `send_tool_name`, so alert dispatch and the poller never spell a backend
   shape themselves.
5. `reply_block` names the tool and the threading identifiers for replying to
   *this* message. It rides in the user turn (never in a cached system block —
   CLAUDE.md's caching rule), which is how the persona can stay backend-neutral.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class MessageRef:
    """A message the poller has not fetched yet."""

    message_id: str
    thread_id: str = ""


@dataclass
class InboundMessage:
    """One fetched inbound message, backend-neutral.

    ``raw_text`` is the backend's own text rendering when it already has the
    RFC-822-ish shape the Executive expects (workspace-mcp's Gmail output);
    when set it is shown verbatim. Otherwise `render_for_executive` composes
    the same shape from the structured fields.
    """

    message_id: str
    thread_id: str
    from_addr: str
    from_name: str = ""
    subject: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    body_text: str = ""
    has_attachments: bool = False
    attachments: list[str] = field(default_factory=list)
    raw_text: str | None = None


_ALL_MARKERS = ("BODY", "ATTACHMENTS", "REPLY")
# On a backend-rendered raw text (Gmail via workspace-mcp) the BODY and
# ATTACHMENTS markers are the server's own and cannot be told apart from a
# forged copy, so only REPLY — which no backend emits and which the persona
# reads the reply tool from — is neutralized there.
_RAW_TEXT_MARKERS = ("REPLY",)


def _neutralize_markers(text: str, markers: tuple[str, ...] = _ALL_MARKERS) -> str:
    """Defang section markers inside inbound text so a body cannot forge a
    ``--- REPLY ---`` block of its own ahead of the real one (the persona
    reads the reply tool from that block). Quoted, not removed — the
    Executive still sees what the sender wrote."""
    pattern = re.compile(r"^(\s*)---\s*(" + "|".join(markers) + r")\s*---", re.IGNORECASE)
    return "\n".join(pattern.sub(r"\1> --- \2 ---", line) for line in text.splitlines())


def _header_safe(value: str) -> str:
    """One header line: no CR/LF (a display name must not inject a header)."""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def render_for_executive(msg: InboundMessage, reply_block: str) -> str:
    """The text handed to `Executive.chat()` for an inbound message.

    Same shape for every backend: ``Subject:`` / ``From:`` / ``To:`` / ``Cc:``
    header lines, a blank line, ``--- BODY ---``, the body, an optional
    ``--- ATTACHMENTS ---`` section, then a ``--- REPLY ---`` block naming the
    reply tool and its threading identifiers. `email_poller._parse_recipients`
    reads the To/Cc lines (it stops at the first blank line), and the persona's
    inbound-email instructions refer to the ATTACHMENTS and REPLY markers.
    """
    if msg.raw_text is not None:
        text = _neutralize_markers(msg.raw_text.rstrip("\n"), _RAW_TEXT_MARKERS)
    else:
        sender = _header_safe(msg.from_addr)
        if msg.from_name:
            sender = f"{_header_safe(msg.from_name)} <{sender}>"
        lines = [f"Subject: {_header_safe(msg.subject)}", f"From: {sender}"]
        if msg.to:
            lines.append("To: " + ", ".join(_header_safe(a) for a in msg.to))
        if msg.cc:
            lines.append("Cc: " + ", ".join(_header_safe(a) for a in msg.cc))
        lines += ["", "--- BODY ---", _neutralize_markers(msg.body_text.rstrip("\n"))]
        if msg.has_attachments or msg.attachments:
            lines += ["", "--- ATTACHMENTS ---", *msg.attachments]
        text = "\n".join(lines)
    return f"{text}\n\n--- REPLY ---\n{reply_block.rstrip()}\n"


@runtime_checkable
class MailProvider(Protocol):
    """Protocol every mail backend satisfies. See the module docstring for the
    invariants; `google.GoogleMail` and `microsoft.MicrosoftMail` implement it."""

    name: str
    server_name: str
    send_tool_name: str
    discovery_queries: tuple[str, ...]

    async def list_unread(self, gateway: Any, mailbox: str, limit: int) -> list[MessageRef]:
        """Unread inbox messages, newest first. ``[]`` on any failure."""
        ...

    async def fetch(self, gateway: Any, ref: MessageRef, mailbox: str) -> InboundMessage | None:
        """The full message, or ``None`` when the backend returned nothing usable."""
        ...

    async def mark_read(self, gateway: Any, ref: MessageRef, mailbox: str) -> None:
        """Mark the message read so the next poll does not see it again. Never raises."""
        ...

    def build_send_arguments(
        self, *, mailbox: str, to: str, subject: str, body: str, thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Arguments for `send_tool_name` — a plain-text message from ``mailbox``."""
        ...

    def reply_block(self, msg: InboundMessage) -> str:
        """The ``--- REPLY ---`` block: which tool replies to ``msg`` and how."""
        ...

    def send_tool_hint(self) -> str:
        """Prose for the scheduler's proactive-email trigger naming the send tool."""
        ...
