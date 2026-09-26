"""Receipt convergence across BOAgents recovery and the Guardian v1 contract."""
from datetime import datetime

import pytest

from openexecutive.bo.execution import engine, store
from openexecutive.bo.execution.synth import SyntheticCounterProvider
from openexecutive.bo.routing import store as outbox

from .test_bo_execution import TENANT, _enable, _mandate, _run, _work
from .test_bo_execution import audit as audit
from .test_bo_execution import db as db


@pytest.mark.parametrize("mode", ["manual", "resume"])
def test_recovery_publishes_newer_correlated_receipt(db, mode):
    _enable()
    run = _run(_mandate())
    provider = SyntheticCounterProvider(idempotent=True, fail_after_write=True)
    _work(provider)
    before = store.list_ledger(TENANT, run["run_id"])[0]
    unknown = next(r["envelope"]["receipt"] for r in outbox.list_outbox(TENANT)
                   if r["envelope"].get("receipt", {}).get("status") == "UNKNOWN")
    if mode == "manual":
        engine.reconcile_run(TENANT, run["run_id"], provider,
                             resolution="receipt", actor="test")
    else:
        engine.resume_run(TENANT, run["run_id"], actor="test")
        _work(provider)
    receipts = [r["envelope"]["receipt"] for r in outbox.list_outbox(TENANT)
                if r["envelope"].get("receipt", {}).get("status") == "CONFIRMED"]
    assert len(receipts) == 1
    confirmed = receipts[0]
    # Guardian orders the verdicts of an attempt by occurredAt, not arrival.
    assert datetime.fromisoformat(confirmed["occurredAt"]) > datetime.fromisoformat(unknown["occurredAt"])
    for key in ("executionRef", "intentRef", "idempotencyKey", "payloadDigest", "attempt"):
        assert confirmed[key] == unknown[key]
    current = store.list_ledger(TENANT, run["run_id"])[0]
    assert confirmed["receiptRef"] == current["receipt_ref"]
    assert current["fence_version"] > before["fence_version"]
    assert provider.total(TENANT) == 1 and provider.submit_calls == 1


def test_reconciliation_time_advances_with_a_stalled_clock(db, monkeypatch):
    _enable()
    run = _run(_mandate())
    provider = SyntheticCounterProvider(idempotent=True, fail_after_write=True)
    _work(provider)
    before = store.list_ledger(TENANT, run["run_id"])[0]
    monkeypatch.setattr(store, "_now", lambda: before["finalized_at"])
    engine.reconcile_run(TENANT, run["run_id"], provider,
                         resolution="receipt", actor="test")
    after = store.list_ledger(TENANT, run["run_id"])[0]
    assert datetime.fromisoformat(after["finalized_at"]) > datetime.fromisoformat(before["finalized_at"])
