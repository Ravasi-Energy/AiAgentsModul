from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openexecutive.prompts.executive_persona import (
    EXECUTIVE_PERSONA_PROMPT,
    MCP_ADDENDUM,
    WEB_SEARCH_ADDENDUM,
)

if TYPE_CHECKING:
    from openexecutive.memory.company_profile import CompanyProfile

KNOWLEDGE_INDEX_SUMMARY = """You have access to a curated knowledge base covering executive frameworks across strategy, finance, HR, legal, operations, marketing, and board communications. When relevant, you retrieve specific frameworks and best practices to ground your analysis. This knowledge base reflects MBA-level and practitioner-level expertise across all core business domains."""

_VOICE_PERSONA_PLACEHOLDER = "{VOICE_PERSONA}"

# Anthropic ignores cache_control on a prefix shorter than the model's minimum
# cacheable length. A marker below the threshold is silently a no-op: no error,
# no cache write, no cache read, and the block bills as ordinary input every
# call — so a marker on a short prompt looks like caching without being it.
MIN_CACHEABLE_TOKENS_SONNET_OPUS = 1024
MIN_CACHEABLE_TOKENS_HAIKU = 2048

# Rough chars-per-token for English prose. Only used to keep prompts on the
# right side of the thresholds above in tests; never for billing.
CHARS_PER_TOKEN_ESTIMATE = 3.7


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for cacheability checks only."""
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE)


def min_cacheable_tokens(model: str) -> int:
    """Minimum cacheable prefix for ``model``. Haiku's is double the rest."""
    return MIN_CACHEABLE_TOKENS_HAIKU if "haiku" in model.lower() else (
        MIN_CACHEABLE_TOKENS_SONNET_OPUS
    )


def build_cacheable_system(
    system_prompt: str,
    model: str,
    *,
    prefix_tokens: int = 0,
    ttl: str | None = None,
) -> str | list[dict[str, Any]]:
    """A system value that carries ``cache_control`` only when it would work.

    Anthropic silently ignores a breakpoint on a prefix below the model's
    minimum — no error, no cache, and the block bills as ordinary input — so a
    marker on a short prompt advertises caching that never happens. Rather than
    asserting a prompt-size range in a comment (which goes stale the moment a
    prompt is edited or an operator sets a long override), measure it.

    ``prefix_tokens`` is anything that precedes the system block in the cached
    prefix — chiefly tool definitions, which count toward the minimum. Returns a
    bare string when the prefix is too short, so callers can pass the result
    straight through as ``system``.
    """
    if estimate_tokens(system_prompt) + prefix_tokens < min_cacheable_tokens(model):
        return system_prompt
    cache_control: dict[str, Any] = {"type": "ephemeral"}
    if ttl is not None:
        cache_control["ttl"] = ttl
    return [{"type": "text", "text": system_prompt, "cache_control": cache_control}]


def estimate_tools_tokens(tools: list[dict[str, Any]] | None) -> int:
    """Rough token cost of serialised tool definitions, for prefix sizing."""
    if not tools:
        return 0
    import json

    return estimate_tokens(json.dumps(tools, sort_keys=True))


def build_system_blocks(
    company_profile: CompanyProfile | None = None,
    mcp_enabled: bool = False,
    persona_override: str | None = None,
    voice_persona_body: str | None = None,
) -> list[dict[str, Any]]:
    """Build system prompt blocks with correct cache_control ordering.

    Block layout (max 2 blocks, total cache_control budget ≤ 2 here so that
    the caller's tool block + message cache stay within the API limit of 4):
      - Block 0: persona + knowledge index (1h TTL, always present)
      - Block 1: company profile + org/dept context (5m TTL, only if non-empty)

    RAG context is injected into the user turn, NOT here.

    persona_override replaces EXECUTIVE_PERSONA_PROMPT when the Agent Council
    has a saved override for the "executive" agent_id.

    voice_persona_body is substituted into the {VOICE_PERSONA} placeholder in
    the assembled base prompt. If the placeholder is absent (user removed it),
    the body is appended at the end of the prompt so it is never silently dropped.
    """
    # Inject user_timezone so the Executive can resolve relative times
    # ("tomorrow 9am") to ISO8601 UTC when calling schedule_followup.
    # Read once from settings — value is process-stable, so cache stays valid.
    from openexecutive.config import get_settings
    settings = get_settings()
    tz = settings.user_timezone
    tz_addendum = f"\n\nThe user's local timezone is {tz} (IANA). When converting relative times to UTC for scheduling, use this zone."

    # The Executive has its own Google Workspace account; without this it
    # falls back to asking the user "what email should I use?" on every
    # Gmail/Calendar/Drive tool call. Process-stable, so cache stays warm.
    # Always appended (even when persona is user-overridden) so a custom
    # persona can never silently drop the bot's own identity.
    exec_email = settings.exec_email_address
    exec_name = settings.exec_display_name
    identity_addendum = (
        "\n\n## Your Identity\n\n"
        f"**You are {exec_name}** — a single AI executive operating on behalf of the "
        "company. When you introduce yourself, sign a message, or set a sender name on "
        f"any outbound communication (email, Slack, Discord, Telegram, calendar invite), "
        f"use **{exec_name}**.\n\n"
        f"**Your email address is {exec_email}.** This mailbox belongs to you — "
        "not to the human you are chatting with. The human has a different email address. "
        f"Do not refer to {exec_email} as the user's email; it is yours.\n\n"
        "When using Gmail, Calendar, Drive, or any Google Workspace tool, act from your own "
        f"account ({exec_email}). Never ask the user which address to send from — always send, "
        "create events, and own documents from your own account. If you need the user's email "
        "or a third party's email, ask for that specifically by name.\n\n"
        "**Never impersonate company personnel.** You are NOT any of the people listed in "
        "the *People You Coordinate With* roster or in the *Leadership* line of the company "
        "profile — not the CEO, not the founder, not any executive or employee, even when "
        "one is tagged `(principal)`. Never sign a message, draft an email, or post a "
        "Slack/Discord/Telegram message under their name. Refer to them in the third person "
        "(e.g. \"Jamie asked…\", \"per Sarah's note…\"). If a message genuinely needs to come "
        "from a specific human, surface that to the user via an alert or ask them to send it "
        f"themselves — do not author it under their name. You always communicate as {exec_name}."
    )

    base_persona = persona_override if persona_override is not None else EXECUTIVE_PERSONA_PROMPT

    # Substitute voice persona body into the {VOICE_PERSONA} placeholder.
    # If absent (user removed it from a custom prompt), append at the end.
    if voice_persona_body is not None:
        if _VOICE_PERSONA_PLACEHOLDER in base_persona:
            base_persona = base_persona.replace(_VOICE_PERSONA_PLACEHOLDER, voice_persona_body)
        else:
            base_persona = base_persona + "\n\n" + voice_persona_body
    else:
        base_persona = base_persona.replace(_VOICE_PERSONA_PLACEHOLDER, "")

    persona = (
        base_persona
        + (WEB_SEARCH_ADDENDUM if settings.enable_web_search else "")
        + (MCP_ADDENDUM if mcp_enabled else "")
        + identity_addendum
        + tz_addendum
    )
    # Knowledge index is appended inline — no separate cache breakpoint needed
    # since it is as stable as the persona (a constant). Keeping them in one
    # block saves a cache_control slot for the caller's tools + message cache.
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": persona + "\n\n" + KNOWLEDGE_INDEX_SUMMARY,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]

    # Combine company profile and org/dept context into a single 5m block.
    # Both change on the same cadence (session/hour) so sharing a breakpoint
    # costs nothing and keeps the total cache_control count at ≤ 2 here.
    context_parts: list[str] = []

    if company_profile is not None:
        profile_block = company_profile.to_prompt_block()
        if profile_block:
            context_parts.append(profile_block)

    from openexecutive.departments.prompt_block import render_org_block

    org_text = render_org_block()
    if org_text:
        context_parts.append(org_text)

    if context_parts:
        blocks.append(
            {
                "type": "text",
                "text": "\n\n".join(context_parts),
                "cache_control": {"type": "ephemeral"},
            },
        )

    return blocks
