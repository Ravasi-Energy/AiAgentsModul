"""Regenerate fixtures/bo/telemetry/valid/*.json from the REAL adapter.

These fixtures are produced — not hand-written — so they can never drift
from what the product actually emits. The script injects a BufferedTransport
into a live TelemetryAdapter and emits one event of every bo.telemetry.v1
kind against a throwaway settings database, then writes the envelopes.

Run from packages/core:

    uv run python scripts/generate_bo_telemetry_fixtures.py

Idempotent: envelope ids/timestamps are regenerated each run; the fixture
contract test only requires schema validity, not byte stability.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "fixtures" / "bo" / "telemetry" / "valid"

import openexecutive.bo.db as bo_db  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="bo-tel-fixtures-"))
bo_db.DB_PATH = _tmp / "bo_agents.db"
bo_db.initialize_db(bo_db.DB_PATH)

from openexecutive.bo.settings import store as settings_store  # noqa: E402
from openexecutive.bo.telemetry.adapter import (  # noqa: E402
    BufferedTransport,
    TelemetryAdapter,
    opaque_actor_ref,
    set_adapter,
)

TENANT = "tenant-fixture"


def main() -> None:
    transport = BufferedTransport()
    adapter = TelemetryAdapter(
        enabled=True, transport=transport,
        producer_id="boagents", installation_id="install-fixture",
    )
    set_adapter(adapter)

    with patch("openexecutive.audit.log_event"):
        # Give the tenant a real config version so the emitted string is
        # the serialized internal version, not the fallback "0".
        settings_store.set_value(TENANT, "bo.ui.language", "ro",
                                 expected_version=0, actor="fixture")
        cv = str(settings_store.config_version(TENANT))

        adapter.emit(tenant=TENANT, kind="Heartbeat", data={
            "sequence": 12, "status": "HEALTHY",
            "observedAt": "2026-09-22T08:00:00Z",
        })
        adapter.emit(tenant=TENANT, kind="RunStarted", run_ref="run_fixture01",
                     agent_ref="bot_fixture01", correlation_id="run_fixture01",
                     data={
                         "trigger": "manual",
                         "definitionRef": "bot_fixture01",
                         "versionNo": 3, "runKind": "simulation",
                     })
        adapter.emit(tenant=TENANT, kind="RunFinished", run_ref="run_fixture01",
                     agent_ref="bot_fixture01", correlation_id="run_fixture01",
                     data={
                         "executionStatus": "SUCCEEDED",
                         "verificationStatus": "VERIFIED",
                         "planHash": "a" * 64,
                         "durationMs": 1.25,
                     })
        adapter.emit(tenant=TENANT, kind="VerificationFinding",
                     run_ref="run_fixture01", agent_ref="bot_fixture01",
                     correlation_id="run_fixture01",
                     data={
                         "findingId": "run_fixture01:heartbeat-stale",
                         "category": "availability", "severity": "MEDIUM",
                         "effectStatus": "NOT_EXECUTED",
                         "ownerRef": opaque_actor_ref("fixture-actor"),
                         "evidenceRefs": ["run_fixture01"],
                     })
        adapter.emit(tenant=TENANT, kind="ConfigApplied", data={
            "key": "bo.ui.language", "configVersion": cv,
            "applyMode": "IMMEDIATE", "appliedVersion": 1,
            "actorRef": opaque_actor_ref("fixture-actor"),
        })

    names = {
        "Heartbeat": "heartbeat.json",
        "RunStarted": "run_started.json",
        "RunFinished": "run_finished.json",
        "VerificationFinding": "verification_finding.json",
        "ConfigApplied": "config_applied.json",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    for event in transport.events:
        path = OUT / names[event["kind"]]
        path.write_text(json.dumps(event, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
