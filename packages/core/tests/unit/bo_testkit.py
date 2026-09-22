"""Shared helpers for the BOAgents Valul 1 test modules."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.bo import db as bo_db


def use_tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the BO database at a per-test file and create the schema."""
    path = tmp_path / "bo_agents.db"
    monkeypatch.setattr(bo_db, "DB_PATH", path)
    bo_db.initialize_db(path)
    return path


def capture_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``audit.log_event`` with a sink; returns the recorded calls."""
    import openexecutive.audit as audit

    events: list[dict[str, Any]] = []

    def _fake(event_type: str, summary: str, **kw: Any) -> None:
        events.append({"event_type": event_type, "summary": summary, **kw})

    monkeypatch.setattr(audit, "log_event", _fake)
    return events
