from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openexecutive.memory.company_profile import CompanyProfile

# Turns of history sent to the model. Beyond this the window SLIDES: the oldest
# exchange is dropped on every subsequent turn, so the message prefix changes
# each request. Prompt caching is prefix-based, so nothing in `messages` is
# cacheable once that starts.
MAX_HISTORY_TURNS = 20


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    company_profile: CompanyProfile | None = None
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    # (channel, channel_ref) pairs the Executive has seen during this session.
    # Used by schedule_followup to refuse scheduling sends to refs the user
    # never actually used — anti-spam guard.
    seen_channel_refs: set[tuple[str, str]] = field(default_factory=set)

    def add_user_message(self, content: str) -> None:
        self.conversation_history.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str | list[dict[str, Any]]) -> None:
        self.conversation_history.append({"role": "assistant", "content": content})

    def history_window_is_stable(self, max_turns: int = MAX_HISTORY_TURNS) -> bool:
        """True while the history window has not started sliding.

        Once ``conversation_history`` outgrows the window, ``get_recent_history``
        drops the oldest exchange every turn, so ``messages[0]`` differs from the
        previous request and a prompt-cache prefix can never match. Marking a
        block ephemeral in that state buys nothing and costs a cache WRITE (more
        than plain input) on every turn, so callers check this first.
        """
        return len(self.conversation_history) <= max_turns * 2

    def get_recent_history(self, max_turns: int = MAX_HISTORY_TURNS) -> list[dict[str, Any]]:
        history = self.conversation_history[-(max_turns * 2):]
        # Anthropic requires messages to start with a user turn.
        # Drop a leading assistant message if history length is odd (can happen on error recovery).
        if history and history[0]["role"] != "user":
            history = history[1:]
        return history
