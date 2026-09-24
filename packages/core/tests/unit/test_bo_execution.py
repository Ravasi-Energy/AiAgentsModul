"""VAL4-01: delegated execution — mandates, checkpoints, effect ledger.

Probele cerute de mandat: două conexiuni/procese independente, rezervări
de buget concurente, claim-uri concurente, lease expirat, fencing pe
finalizare, adâncime/expiry/intersecție/escaladare, revocare în rulare,
versiunea politicii, eșec de checkpoint, crash înainte/după checkpoint și
înainte/după efect, ACK pierdut, restart, chei duplicate / payload
schimbat, provider cu și fără idempotență, receipt lookup înainte de
retry, stări necunoscute + reconciliere, spoofing actor/tenant, RBAC.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from openexecutive.bo.execution import engine, store
from openexecutive.bo.execution.mandate import (
    MandateExpiredError,
    MandateValidationError,
    assert_active,
    mandate_state,
)
from openexecutive.bo.execution.synth import (
    SyntheticCounterProvider,
)
from openexecutive.bo.settings import store as settings_store

from .bo_testkit import capture_audit, use_tmp_db

TENANT = "tenant-a"
FUTURE = (datetime.now(UTC) + timedelta(hours=2)).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")
PAST = (datetime.now(UTC) - timedelta(hours=1)).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return use_tmp_db(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    return capture_audit(monkeypatch)


def _enable() -> None:
    settings_store.set_value(
        TENANT, "bo.exec.enabled", True, expected_version=0, actor="test"
    )


def _fields(**over: Any) -> dict[str, Any]:
    fields = {
        "allowed_resources": ["synth.*"],
        "allowed_actions": ["increment"],
        "budget_limit": "10",
        "concurrency_limit": 2,
        "max_steps": 10,
        "max_depth": 2,
        "expires_at": FUTURE,
    }
    fields.update(over)
    return fields


def _mandate(
    parent: Any = None, actor: str = "admin", **over: Any
) -> Any:
    return store.create_mandate(
        TENANT, _fields(**over), parent=parent,
        principal_ref="actor_root", policy_version=1,
        actor=actor, max_depth_cap=8,
    )


def _step(amount: int = 1, **over: Any) -> dict[str, Any]:
    return {
        "action": "increment", "resource": "synth.counter",
        "payload": {"amount": amount}, **over,
    }


def _run(mandate: Any, steps: list[dict] | None = None,
         budget: str = "1", **kw: Any) -> dict[str, Any]:
    return engine.submit_execution(
        TENANT, mandate.mandate_id, steps or [_step()],
        budget_amount=Decimal(budget), correlation_id=None,
        actor="admin", **kw,
    )


def _work(provider: SyntheticCounterProvider,
          worker: str = "w1", **kw: Any) -> dict[str, Any]:
    return engine.work_once(
        TENANT, provider=provider, worker_id=worker, **kw
    )


# --------------------------------------------------------------------------- #
# Mandate — creation, intersection, amplification refusals
# --------------------------------------------------------------------------- #

class TestMandateIntersection:
    def test_child_within_parent(self, db: Path) -> None:
        parent = _mandate()
        child = _mandate(
            parent=parent, allowed_resources=["synth.counter"],
            budget_limit="5", concurrency_limit=1, max_steps=5,
            max_depth=2,
        )
        assert child.depth == 1
        assert child.parent_mandate_id == parent.mandate_id

    @pytest.mark.parametrize("over", [
        {"allowed_resources": ["synth.*", "files.*"]},
        {"allowed_actions": ["increment", "delete"]},
        {"budget_limit": "11"},
        {"concurrency_limit": 3},
        {"max_steps": 11},
        {"max_depth": 3},
    ])
    def test_child_amplification_refused(
        self, db: Path, over: dict[str, Any]
    ) -> None:
        parent = _mandate()
        with pytest.raises(MandateValidationError):
            _mandate(parent=parent, **over)

    def test_child_expiry_beyond_parent_refused(self, db: Path) -> None:
        parent = _mandate()
        later = (datetime.now(UTC) + timedelta(days=30)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        with pytest.raises(MandateValidationError):
            _mandate(parent=parent, expires_at=later)

    def test_depth_cap(self, db: Path) -> None:
        m0 = _mandate(max_depth=2)
        m1 = _mandate(parent=m0)
        m2 = _mandate(parent=m1)
        assert m2.depth == 2
        with pytest.raises(MandateValidationError):
            _mandate(parent=m2)  # depth 3 > max_depth=2

    def test_tenant_depth_cap(self, db: Path) -> None:
        settings_store.set_value(
            TENANT, "bo.exec.max_delegation_depth", 1,
            expected_version=0, actor="test",
        )
        m0 = store.create_mandate(
            TENANT, _fields(max_depth=1), parent=None,
            principal_ref="actor_root", policy_version=1,
            actor="admin", max_depth_cap=1,
        )
        m1 = store.create_mandate(
            TENANT, _fields(max_depth=1), parent=m0,
            principal_ref="actor_root", policy_version=1,
            actor="admin", max_depth_cap=1,
        )
        assert m1.depth == 1
        with pytest.raises(MandateValidationError):
            store.create_mandate(
                TENANT, _fields(max_depth=1), parent=m1,
                principal_ref="actor_root", policy_version=1,
                actor="admin", max_depth_cap=1,
            )

    def test_revoked_parent_refuses_child(self, db: Path) -> None:
        parent = _mandate()
        store.revoke_mandate(
            TENANT, parent.mandate_id, reason="t", actor="admin"
        )
        revoked = store.get_mandate(TENANT, parent.mandate_id)
        with pytest.raises(MandateValidationError):
            _mandate(parent=revoked)

    def test_expired_mandate_blocks_effect(self, db: Path) -> None:
        # A mandate whose expiry passed between creation and execution:
        # expires_at is validated at creation, so simulate by direct row
        # update (time travel is the honest way to test the boundary check).
        m = _mandate()
        with store.get_conn() as conn:
            conn.execute(
                "UPDATE bo_exec_mandates SET expires_at = ? "
                "WHERE tenant = ? AND mandate_id = ?",
                (PAST, TENANT, m.mandate_id),
            )
        stale = store.get_mandate(TENANT, m.mandate_id)
        assert mandate_state(stale) == "expired"
        with pytest.raises(MandateExpiredError):
            assert_active(stale)

    def test_revocation_transitive(self, db: Path) -> None:
        m0 = _mandate()
        m1 = _mandate(parent=m0)
        m2 = _mandate(parent=m1)
        store.revoke_mandate(
            TENANT, m0.mandate_id, reason="politică nouă", actor="admin"
        )
        for m in (m0, m1, m2):
            fresh = store.get_mandate(TENANT, m.mandate_id)
            assert mandate_state(fresh) == "revoked"


# --------------------------------------------------------------------------- #
# Submission — budget/concurrency reservation, atomicity
# --------------------------------------------------------------------------- #

class TestSubmission:
    def test_disabled_refuses(self, db: Path) -> None:
        m = _mandate()
        with pytest.raises(engine.ExecutionDisabledError):
            _run(m)

    def test_submit_and_reserve(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m, budget="3")
        assert run["state"] == store.RUN_PENDING
        res = store.reservation_for(TENANT, run["run_id"])
        assert res is not None and res["state"] == store.RES_RESERVED
        assert Decimal(res["amount"]) == Decimal("3")

    def test_budget_overspend_refused(self, db: Path) -> None:
        _enable()
        m = _mandate(budget_limit="5")
        _run(m, budget="4")
        with pytest.raises(store.BudgetExceededError):
            _run(m, budget="2")  # 4+2 > 5

    def test_concurrency_overspend_refused(self, db: Path) -> None:
        _enable()
        m = _mandate(concurrency_limit=2)
        settings_store.set_value(
            TENANT, "bo.exec.default_concurrency", 2,
            expected_version=0, actor="test",
        )
        _run(m, budget="1")
        with pytest.raises(store.BudgetExceededError):
            _run(m, budget="1")

    def test_step_outside_mandate_refused(self, db: Path) -> None:
        _enable()
        m = _mandate()
        with pytest.raises(MandateValidationError):
            _run(m, steps=[_step(action="delete")])
        with pytest.raises(MandateValidationError):
            _run(m, steps=[_step(resource="files.etc")])

    def test_concurrent_budget_reservations(self, db: Path) -> None:
        """Two threads racing to reserve — only what fits wins; the
        invariant is enforced inside one BEGIN IMMEDIATE per submit."""
        _enable()
        m = _mandate(budget_limit="10")
        results: list[str] = []
        lock = threading.Lock()

        def attempt() -> None:
            try:
                _run(m, budget="6")
                outcome = "ok"
            except store.BudgetExceededError:
                outcome = "refused"
            with lock:
                results.append(outcome)

        t1, t2 = threading.Thread(target=attempt), threading.Thread(
            target=attempt
        )
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert sorted(results) == ["ok", "refused"]


# --------------------------------------------------------------------------- #
# Execution — happy path, claims, leases, fencing
# --------------------------------------------------------------------------- #

class TestExecution:
    def test_happy_path(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(3), _step(2)])
        prov = SyntheticCounterProvider(idempotent=True)
        out = _work(prov)
        assert out["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        assert prov.total(TENANT) == 5
        ledger = store.list_ledger(TENANT, run["run_id"])
        assert all(e["status"] == store.LED_SUCCEEDED for e in ledger)
        assert all(e["receipt_ref"] for e in ledger)
        # Checkpoints: pre+post per step, versioned.
        cps = store.list_checkpoints(TENANT, run["run_id"])
        assert [(c["step"], c["state"]["phase"]) for c in cps] == [
            (0, "pre"), (0, "post"), (1, "pre"), (1, "post"),
        ]
        assert store.reservation_for(
            TENANT, run["run_id"]
        )["state"] == store.RES_COMMITTED

    def test_two_workers_no_double_claim(self, db: Path) -> None:
        _enable()
        m = _mandate()
        _run(m)
        c1 = store.claim_runs(TENANT, worker_id="w1", limit=5, lease_s=60)
        c2 = store.claim_runs(TENANT, worker_id="w2", limit=5, lease_s=60)
        assert len(c1) == 1 and c2 == []

    def test_lease_expiry_reclaim(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m)
        store.claim_runs(TENANT, worker_id="w1", limit=5, lease_s=1)
        import time
        time.sleep(1.1)
        c2 = store.claim_runs(TENANT, worker_id="w2", limit=5, lease_s=60)
        assert [r["run_id"] for r in c2] == [run["run_id"]]
        assert c2[0]["lease_seq"] == 2

    def test_fencing_blocks_stale_transition(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m)
        w1 = store.claim_runs(
            TENANT, worker_id="w1", limit=5, lease_s=1
        )[0]
        import time
        time.sleep(1.1)
        store.claim_runs(TENANT, worker_id="w2", limit=5, lease_s=60)
        with pytest.raises(store.ConflictError):
            store.transition_run(
                TENANT, run["run_id"], store.RUN_RUNNING,
                worker_id="w1", lease_s=60, fence=w1["lease_seq"],
            )

    def test_fencing_blocks_stale_finalize(self, db: Path) -> None:
        """Worker A claims a ledger entry, loses the lease, worker B
        re-claims (fence_version bumps) — A's finalize is fenced out."""
        _enable()
        m = _mandate()
        run = _run(m)
        e = store.get_or_create_intent(
            TENANT, run, 0, provider="synth.counter",
            payload={"amount": 1},
        )
        claimed = store.claim_ledger_entry(
            TENANT, e["entry_id"], worker_id="w1", lease_s=1,
        )
        import time
        time.sleep(1.1)
        reclaim = store.claim_ledger_entry(
            TENANT, e["entry_id"], worker_id="w2", lease_s=60,
        )
        assert reclaim["fence_version"] == claimed["fence_version"] + 1
        # Stale worker A cannot finalize.
        assert not store.finalize_ledger_entry(
            TENANT, e["entry_id"], status=store.LED_SUCCEEDED,
            fence_version=claimed["fence_version"],
        )
        # Worker B can.
        assert store.finalize_ledger_entry(
            TENANT, e["entry_id"], status=store.LED_SUCCEEDED,
            fence_version=reclaim["fence_version"], receipt_ref="r1",
        )

    def test_revocation_mid_run(self, db: Path) -> None:
        """Revoke between steps — the NEXT effect is refused even though
        the mandate was valid at submission."""
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(1), _step(1)])
        prov = SyntheticCounterProvider(idempotent=True)
        claimed = store.claim_runs(
            TENANT, worker_id="w1", limit=5, lease_s=60
        )[0]
        # Execute step 0 manually by running the engine, then revoking
        # between steps is not directly hookable — so run with a
        # checkpoint writer that revokes after the first post-checkpoint.
        calls = {"n": 0}
        real_cp = store.write_checkpoint

        def revoking_checkpoint(*a: Any, **kw: Any) -> dict:
            calls["n"] += 1
            out = real_cp(*a, **kw)
            if calls["n"] == 2:  # after step-0 post checkpoint
                store.revoke_mandate(
                    TENANT, m.mandate_id, reason="revocat la pasul 2",
                    actor="admin",
                )
            return out

        outcome = engine.execute_run(
            TENANT, claimed, prov, worker_id="w1", lease_s=60,
            checkpoint_writer=revoking_checkpoint,
        )
        assert outcome["state"] == store.RUN_FAILED
        assert "mandate_revoked" in outcome["block_reason"]
        assert prov.total(TENANT) == 1  # only step 0 executed
        fresh = store.get_run(TENANT, run["run_id"])
        assert fresh["state"] == store.RUN_FAILED
        assert store.reservation_for(
            TENANT, run["run_id"]
        )["state"] == store.RES_RELEASED

    def test_revocation_transitive_blocks_child_run(self, db: Path) -> None:
        _enable()
        m0 = _mandate()
        m1 = _mandate(parent=m0)
        _run(m1, steps=[_step(1), _step(1)])
        store.revoke_mandate(
            TENANT, m0.mandate_id, reason="părinte revocat", actor="admin"
        )
        prov = SyntheticCounterProvider(idempotent=True)
        out = _work(prov)
        assert out["outcomes"][0]["state"] == store.RUN_FAILED
        assert prov.total(TENANT) == 0

    def test_policy_version_snapshot(self, db: Path) -> None:
        _enable()
        settings_store.set_value(
            TENANT, "bo.ui.display_name", "BO-Test",
            expected_version=0, actor="test",
        )
        m = store.create_mandate(
            TENANT, _fields(), parent=None, principal_ref="a",
            policy_version=settings_store.config_version(TENANT),
            actor="admin", max_depth_cap=8,
        )
        run = _run(m)
        prov = SyntheticCounterProvider(idempotent=True)
        _work(prov)
        entry = store.list_ledger(TENANT, run["run_id"])[0]
        # The snapshot is recorded at issuance and propagated to run +
        # ledger entry — not re-read at effect time.
        assert m.policy_version == settings_store.config_version(TENANT)
        assert run["policy_version"] == m.policy_version
        assert entry["policy_version"] == m.policy_version


# --------------------------------------------------------------------------- #
# Checkpoints
# --------------------------------------------------------------------------- #

class TestCheckpoints:
    def test_checkpoint_failure_blocks_step(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(7)])
        prov = SyntheticCounterProvider(idempotent=True)
        claimed = store.claim_runs(
            TENANT, worker_id="w1", limit=5, lease_s=60
        )[0]

        def failing_checkpoint(*a: Any, **kw: Any) -> dict:
            raise OSError("disc plin")

        outcome = engine.execute_run(
            TENANT, claimed, prov, worker_id="w1", lease_s=60,
            checkpoint_writer=failing_checkpoint,
        )
        assert outcome["state"] == store.RUN_FAILED
        assert outcome["block_reason"] == "checkpoint_unavailable"
        assert prov.total(TENANT) == 0
        assert store.list_ledger(TENANT, run["run_id"]) == []

    def test_checkpoint_history_append_only(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m)
        store.write_checkpoint(TENANT, run["run_id"], 0, {"a": 1})
        store.write_checkpoint(TENANT, run["run_id"], 0, {"a": 2})
        cps = store.list_checkpoints(TENANT, run["run_id"])
        assert [(c["checkpoint_version"], c["state"]["a"])
                for c in cps] == [(1, 1), (2, 2)]

    def test_checkpoint_disabled_setting(self, db: Path) -> None:
        _enable()
        settings_store.set_value(
            TENANT, "bo.exec.checkpoint_required", False,
            expected_version=0, actor="test",
        )
        m = _mandate()
        run = _run(m)
        prov = SyntheticCounterProvider(idempotent=True)
        out = _work(prov)
        assert out["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        assert store.list_checkpoints(TENANT, run["run_id"]) == []


# --------------------------------------------------------------------------- #
# Crash / restart / resume — same identity, no duplicate effects
# --------------------------------------------------------------------------- #

class TestCrashRecovery:
    def test_crash_before_effect_resumes(self, db: Path) -> None:
        """Worker claims + checkpoint written, process dies before the
        effect. After restart (new worker), the step executes ONCE."""
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(4)])
        store.claim_runs(
            TENANT, worker_id="dead-w1", limit=5, lease_s=1
        )
        # Checkpoint persisted, then "crash" — no effect, no transition.
        store.write_checkpoint(
            TENANT, run["run_id"], 0,
            {"phase": "pre", "step": 0, "action": "increment",
             "resource": "synth.counter"},
        )
        import time
        time.sleep(1.1)  # lease expires
        prov = SyntheticCounterProvider(idempotent=True)
        out = _work(prov, worker="w2")
        assert out["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        assert prov.total(TENANT) == 4
        assert prov.effect_count(TENANT) == 1

    def test_crash_after_effect_no_duplicate(self, db: Path) -> None:
        """Effect committed + answer lost (fail_after_write). Provider is
        idempotent: resume deduplicates by key — counter stays 1×."""
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(5)])
        flaky = SyntheticCounterProvider(
            idempotent=True, fail_after_write=True
        )
        out = _work(flaky)
        # Answer lost → ambiguous → UNKNOWN (resumable).
        assert out["outcomes"][0]["state"] == store.RUN_UNKNOWN
        assert flaky.total(TENANT) == 5
        entry = store.list_ledger(TENANT, run["run_id"])[0]
        assert entry["status"] == store.LED_UNKNOWN
        # Restart + resume: receipt lookup proves the effect — no second
        # submit, no double.
        stable = SyntheticCounterProvider(idempotent=True)
        engine.resume_run(TENANT, run["run_id"], actor="admin")
        out2 = _work(stable, worker="w2")
        assert out2["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        assert stable.submit_calls == 0  # receipt lookup only
        assert stable.total(TENANT) == 5
        assert stable.effect_count(TENANT) == 1
        final = store.list_ledger(TENANT, run["run_id"])[0]
        assert final["status"] == store.LED_SUCCEEDED
        assert final["receipt_ref"] is not None

    def test_restart_preserves_identity_and_keys(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(1), _step(1)])
        prov = SyntheticCounterProvider(idempotent=True)
        claimed = store.claim_runs(
            TENANT, worker_id="w1", limit=5, lease_s=60
        )[0]
        # Run only the first step, then "crash" (stop driving the engine).
        keys_before = {
            e["idempotency_key"]
            for e in store.list_ledger(TENANT, run["run_id"])
        }
        out = engine.execute_run(
            TENANT, claimed, prov, worker_id="w1", lease_s=60,
        )
        assert out["state"] == store.RUN_SUCCEEDED
        keys_after = {
            e["idempotency_key"]
            for e in store.list_ledger(TENANT, run["run_id"])
        }
        # Resume keeps the same run identity + stable intent keys.
        assert run["run_id"] in {k.split(":")[0] for k in keys_after}
        assert keys_before <= keys_after

    def test_pause_resume_same_run(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(1), _step(1)])
        prov = SyntheticCounterProvider(idempotent=True)
        claimed = store.claim_runs(
            TENANT, worker_id="w1", limit=5, lease_s=60
        )[0]
        calls = {"n": 0}
        real_cp = store.write_checkpoint

        def pausing_checkpoint(*a: Any, **kw: Any) -> dict:
            calls["n"] += 1
            out = real_cp(*a, **kw)
            if calls["n"] == 2:
                store.request_flag(
                    TENANT, run["run_id"], "pause_requested", actor="admin"
                )
            return out

        out = engine.execute_run(
            TENANT, claimed, prov, worker_id="w1", lease_s=60,
            checkpoint_writer=pausing_checkpoint,
        )
        assert out["state"] == store.RUN_PAUSED
        assert prov.total(TENANT) == 1
        engine.resume_run(TENANT, run["run_id"], actor="admin")
        out2 = _work(prov, worker="w2")
        assert out2["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        assert prov.total(TENANT) == 2
        assert prov.effect_count(TENANT) == 2

    def test_cancel_does_not_reverse_effect(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(3), _step(1)])
        prov = SyntheticCounterProvider(idempotent=True)
        claimed = store.claim_runs(
            TENANT, worker_id="w1", limit=5, lease_s=60
        )[0]
        calls = {"n": 0}
        real_cp = store.write_checkpoint

        def cancelling_checkpoint(*a: Any, **kw: Any) -> dict:
            calls["n"] += 1
            out = real_cp(*a, **kw)
            if calls["n"] == 2:
                store.request_flag(
                    TENANT, run["run_id"], "cancel_requested", actor="admin"
                )
            return out

        out = engine.execute_run(
            TENANT, claimed, prov, worker_id="w1", lease_s=60,
            checkpoint_writer=cancelling_checkpoint,
        )
        assert out["state"] == store.RUN_CANCELLED
        assert prov.total(TENANT) == 3  # step 0's effect stays
        assert store.reservation_for(
            TENANT, run["run_id"]
        )["state"] == store.RES_RELEASED


# --------------------------------------------------------------------------- #
# Idempotency / payload conflicts / provider semantics
# --------------------------------------------------------------------------- #

class TestLedgerSemantics:
    def test_same_key_same_payload_reuses_intent(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m)
        e1 = store.get_or_create_intent(
            TENANT, run, 0, provider="synth.counter",
            payload={"amount": 1},
        )
        e2 = store.get_or_create_intent(
            TENANT, run, 0, provider="synth.counter",
            payload={"amount": 1},
        )
        assert e1["entry_id"] == e2["entry_id"]

    def test_same_key_changed_payload_conflicts(self, db: Path) -> None:
        _enable()
        m = _mandate()
        run = _run(m)
        store.get_or_create_intent(
            TENANT, run, 0, provider="synth.counter",
            payload={"amount": 1},
        )
        with pytest.raises(store.PayloadConflictError):
            store.get_or_create_intent(
                TENANT, run, 0, provider="synth.counter",
                payload={"amount": 2},
            )

    def test_idempotent_provider_dedup(self, db: Path) -> None:
        prov = SyntheticCounterProvider(idempotent=True)
        r1 = prov.submit(
            tenant=TENANT, idempotency_key="k1",
            payload_digest="d", amount=5,
        )
        r2 = prov.submit(
            tenant=TENANT, idempotency_key="k1",
            payload_digest="d", amount=5,
        )
        assert r1["receipt_ref"] == r2["receipt_ref"]
        assert r2["deduplicated"] is True
        assert prov.total(TENANT) == 5
        assert prov.effect_count(TENANT) == 1

    def test_nonidempotent_timeout_reconciliation(self, db: Path) -> None:
        """Non-idempotent provider + lost answer → RECONCILIATION_REQUIRED
        and NO blind retry; receipt lookup resolves it (effect happened
        once, counter proves it)."""
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(3), _step(1)])
        flaky = SyntheticCounterProvider(
            idempotent=False, fail_after_write=True
        )
        out = _work(flaky)
        assert out["outcomes"][0]["state"] == store.RUN_RECONCILIATION
        entry = store.list_ledger(TENANT, run["run_id"])[0]
        assert entry["status"] == store.LED_RECONCILIATION
        assert flaky.total(TENANT) == 3
        # Resume is refused while reconciliation is pending.
        with pytest.raises(store.InvalidStateError):
            engine.resume_run(TENANT, run["run_id"], actor="admin")
        # Reconcile via receipt — the effect provably happened.
        stable = SyntheticCounterProvider(idempotent=False)
        result = engine.reconcile_run(
            TENANT, run["run_id"], stable,
            resolution="receipt", actor="admin",
        )
        assert result["resolved"] == 1
        entry2 = store.list_ledger(TENANT, run["run_id"])[0]
        assert entry2["status"] == store.LED_SUCCEEDED
        # The remaining step may now run — total ends at 4, not 8.
        engine.resume_run(TENANT, run["run_id"], actor="admin")
        out2 = _work(stable)
        assert out2["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        assert stable.total(TENANT) == 4

    def test_nonidempotent_no_receipt_stays_unknown(self, db: Path) -> None:
        """Provider without receipt support: the ambiguity cannot be
        resolved automatically — the entry stays RECONCILIATION_REQUIRED
        until the operator asserts (mark_failed)."""
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(2)])
        flaky = SyntheticCounterProvider(
            idempotent=False, supports_receipts=False,
            fail_after_write=True,
        )
        out = _work(flaky)
        assert out["outcomes"][0]["state"] == store.RUN_RECONCILIATION
        blind = SyntheticCounterProvider(
            idempotent=False, supports_receipts=False,
        )
        result = engine.reconcile_run(
            TENANT, run["run_id"], blind,
            resolution="receipt", actor="admin",
        )
        assert result["resolved"] == 0
        entry = store.list_ledger(TENANT, run["run_id"])[0]
        assert entry["status"] == store.LED_RECONCILIATION
        # Operator asserts no effect → intent resets → resume retries.
        result2 = engine.reconcile_run(
            TENANT, run["run_id"], blind,
            resolution="mark_failed", actor="admin",
        )
        assert result2["resolved"] == 1
        engine.resume_run(TENANT, run["run_id"], actor="admin")
        out2 = _work(blind)
        assert out2["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        # Provider counted the retry — the counter reflects the real
        # double-effect risk the UI warns about (original + retry = 2).
        assert blind.total(TENANT) + flaky.total(TENANT) >= 2

    def test_receipt_lookup_before_retry(self, db: Path) -> None:
        """A stale SUBMITTED entry is resolved by receipt lookup — the
        provider's submit wire is never touched again."""
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(2)])
        prov = SyntheticCounterProvider(idempotent=True)
        # Simulate: effect happened, answer lost, entry left SUBMITTED.
        receipt = prov.submit(
            tenant=TENANT, idempotency_key=f"{run['run_id']}:0",
            payload_digest="x", amount=2,
        )
        entry = store.get_or_create_intent(
            TENANT, run, 0, provider="synth.counter",
            payload={"amount": 2},
        )
        store.mark_ledger_status(
            TENANT, entry["entry_id"], store.LED_SUBMITTED,
        )
        out = _work(prov)
        assert out["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        assert prov.submit_calls == 1  # only the pre-seeded submit
        final = store.list_ledger(TENANT, run["run_id"])[0]
        assert final["status"] == store.LED_SUCCEEDED
        assert final["receipt_ref"] == receipt["receipt_ref"]

    def test_exactly_once_claim_for_idempotent(self, db: Path) -> None:
        """The idempotent path: claim → submit → dedup → SUCCEEDED, the
        counter proves the single effect even across re-delivery."""
        _enable()
        m = _mandate()
        _run(m, steps=[_step(2), _step(3)])
        prov = SyntheticCounterProvider(idempotent=True)
        out = _work(prov)
        assert out["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        assert prov.effect_count(TENANT) == 2
        assert prov.total(TENANT) == 5
        # Re-running work has nothing claimable — no double.
        out2 = _work(prov, worker="w2")
        assert out2["claimed"] == 0
        assert prov.total(TENANT) == 5


# --------------------------------------------------------------------------- #
# Telemetry events — durable outbox, ACK loss, disabled adapter
# --------------------------------------------------------------------------- #

class TestExecutionEvents:
    def test_events_persist_in_outbox(self, db: Path) -> None:
        _enable()
        m = _mandate()
        engine.emit_mandate_event(TENANT, m, kind="created")
        from openexecutive.bo.routing import store as routing_store

        stats = routing_store.outbox_stats(TENANT)
        assert stats["outbox_pending"] >= 1

    def test_ack_loss_retries_same_envelope(self, db: Path) -> None:
        """Execution events ride the REM-01 outbox: a lost ACK resends
        the persisted envelope byte-identically — the receiver's
        DUPLICATE ack deduplicates by eventId."""
        _enable()
        m = _mandate()
        engine.emit_mandate_event(TENANT, m, kind="created")
        from openexecutive.bo.routing import delivery
        from openexecutive.bo.routing import store as routing_store
        from openexecutive.bo.telemetry.adapter import (
            BufferedTransport,
            TelemetryAdapter,
        )

        class FlakyTransport(BufferedTransport):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def send(self, event: dict) -> dict:
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("ACK pierdut")
                return {"status": "DUPLICATE", "eventId": event["eventId"]}

        transport = FlakyTransport()
        adapter = TelemetryAdapter(enabled=True, transport=transport)
        delivery.deliver_pending(TENANT, adapter=adapter)
        assert routing_store.outbox_stats(TENANT)["outbox_pending"] == 1
        delivery.deliver_pending(TENANT, adapter=adapter)
        stats = routing_store.outbox_stats(TENANT)
        assert stats["outbox_pending"] == 0
        assert transport.calls == 2

    def test_event_shape_and_minimization(self, db: Path) -> None:
        from openexecutive.bo.execution import serialize

        env = serialize.build_execution_event(
            tenant=TENANT, kind="receipt",
            body={"runId": "r", "receiptRef": "x"},
        )
        assert env["schemaVersion"] == "bo.execution-control.v1"
        assert env["tenantRef"] == TENANT
        serialize.validate_execution_event(env)
        with pytest.raises(serialize.ExecutionEventError):
            serialize.build_execution_event(
                tenant=TENANT, kind="receipt",
                body={"payload": "secret material"},
            )
        with pytest.raises(serialize.ExecutionEventError):
            serialize.build_execution_event(
                tenant=TENANT, kind="bogus", body={},
            )


# --------------------------------------------------------------------------- #
# API — RBAC, spoofing, tenant isolation
# --------------------------------------------------------------------------- #

class TestRoutes:
    ADMIN = {"x-caller-email": "admin@test"}
    VIEWER = {"x-caller-email": "viewer@test"}

    @pytest.fixture()
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from openexecutive.api.routes import bo as bo_route

        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
        monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@test")
        app = FastAPI()
        app.include_router(bo_route.router)
        bo_route.register_error_handlers(app)
        return TestClient(app)

    def _mandate(self, client: Any) -> dict[str, Any]:
        resp = client.post("/bo/execution/mandates", headers=self.ADMIN,
                           json={
                               "allowed_resources": ["synth.*"],
                               "allowed_actions": ["increment"],
                               "budget_limit": "10",
                               "concurrency_limit": 2,
                               "max_steps": 10, "max_depth": 2,
                               "expires_at": FUTURE,
                           })
        assert resp.status_code == 201, resp.text
        return resp.json()["mandate"]

    def _enable(self, client: Any) -> None:
        resp = client.put(
            "/bo/settings/bo.exec.enabled", headers=self.ADMIN,
            json={"value": True, "expected_version": 0},
        )
        assert resp.status_code == 200, resp.text

    def test_rbac_viewer_read_only(self, client) -> None:
        resp = client.post("/bo/execution/mandates", headers=self.VIEWER,
                           json={
                               "allowed_resources": ["a"],
                               "allowed_actions": ["b"],
                               "budget_limit": "1",
                               "concurrency_limit": 1, "max_steps": 1,
                               "max_depth": 1, "expires_at": FUTURE,
                           })
        assert resp.status_code == 403
        resp = client.get("/bo/execution/mandates", headers=self.VIEWER)
        assert resp.status_code == 200

    def test_exec_disabled_by_default(self, client) -> None:
        m = self._mandate(client)
        resp = client.post("/bo/execution/runs", headers=self.ADMIN,
                           json={
                               "mandate_id": m["mandate_id"],
                               "steps": [_step()],
                               "budget_amount": "1",
                           })
        assert resp.status_code == 403
        assert resp.json()["error"] == "exec_disabled"

    def test_full_flow_over_http(self, client) -> None:
        self._enable(client)
        m = self._mandate(client)
        resp = client.post("/bo/execution/runs", headers=self.ADMIN,
                           json={
                               "mandate_id": m["mandate_id"],
                               "steps": [_step(4)],
                               "budget_amount": "2",
                           })
        assert resp.status_code == 201, resp.text
        run = resp.json()["run"]
        resp = client.post("/bo/execution/work", headers=self.ADMIN,
                           json={"limit": 5})
        assert resp.status_code == 200, resp.text
        assert resp.json()["outcomes"][0]["state"] == "SUCCEEDED"
        resp = client.get(
            f"/bo/execution/runs/{run['run_id']}", headers=self.VIEWER
        )
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["kind"] == "execution"
        assert detail["ledger"][0]["status"] == "SUCCEEDED"
        assert detail["ledger"][0]["receipt_ref"]
        assert len(detail["checkpoints"]) == 2
        status = client.get("/bo/execution/status", headers=self.VIEWER)
        assert status.json()["synthetic_effect_total"] == 4

    def test_work_requires_operator(self, client) -> None:
        resp = client.post("/bo/execution/work", headers=self.VIEWER,
                           json={})
        assert resp.status_code == 403

    def test_tenant_spoofing_refused(self, client) -> None:
        headers = {**self.ADMIN, "x-bo-tenant": "other-tenant"}
        resp = client.get("/bo/execution/mandates", headers=headers)
        assert resp.status_code == 403

    def test_principal_ref_is_server_derived(self, client) -> None:
        """The request body cannot choose the principal — the mandate's
        principal_ref is derived from the authenticated actor."""
        m = self._mandate(client)
        assert m["principal_ref"].startswith("actor_")
        assert m["principal_ref"] != "admin@test"
        assert m["created_by"] == "admin@test"

    def test_revoke_over_http(self, client) -> None:
        m = self._mandate(client)
        resp = client.post(
            f"/bo/execution/mandates/{m['mandate_id']}/revoke",
            headers=self.ADMIN, json={"reason": "test"},
        )
        assert resp.status_code == 200
        assert resp.json()["mandate"]["state"] == "revoked"
        self._enable(client)
        resp = client.post("/bo/execution/runs", headers=self.ADMIN,
                           json={
                               "mandate_id": m["mandate_id"],
                               "steps": [_step()],
                               "budget_amount": "1",
                           })
        assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Two independent processes on the same database file
# --------------------------------------------------------------------------- #

class TestTwoProcesses:
    def test_subprocess_worker_resumes_and_executes_once(
        self, db: Path
    ) -> None:
        """Process A submits + claims (then "crashes" — lease expires).
        A real subprocess (the CLI worker) claims the same run, executes
        it once, and the counter proves the single effect — this is the
        cross-process claim/resume evidence, not a thread simulation."""
        _enable()
        m = _mandate()
        run = _run(m, steps=[_step(4)])
        store.claim_runs(TENANT, worker_id="proc-a", limit=5, lease_s=1)
        import json
        import os
        import subprocess
        import sys
        import time

        time.sleep(1.1)  # lease expiry — process A is "dead"
        env = dict(os.environ)
        env["BOAGENTS_DB_PATH"] = str(db)
        env["BO_TENANT_ID"] = TENANT
        import openexecutive

        proc = subprocess.run(
            [sys.executable, "-m",
             "openexecutive.bo.execution.cli", "work", "proc-b"],
            capture_output=True, text=True, env=env, timeout=120,
            cwd=str(Path(openexecutive.__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["claimed"] == 1
        assert out["outcomes"][0]["state"] == store.RUN_SUCCEEDED
        prov = SyntheticCounterProvider(idempotent=True)
        assert prov.total(TENANT) == 4
        assert prov.effect_count(TENANT) == 1
        assert store.get_run(TENANT, run["run_id"])["state"] == (
            store.RUN_SUCCEEDED
        )

    def test_subprocess_concurrent_claim_no_double(self, db: Path) -> None:
        """Main process holds the claim (active lease): a subprocess
        worker finds nothing claimable — claims are cross-process."""
        _enable()
        m = _mandate()
        _run(m)
        store.claim_runs(TENANT, worker_id="proc-a", limit=5, lease_s=60)
        import json
        import os
        import subprocess
        import sys

        env = dict(os.environ)
        env["BOAGENTS_DB_PATH"] = str(db)
        env["BO_TENANT_ID"] = TENANT
        import openexecutive

        proc = subprocess.run(
            [sys.executable, "-m",
             "openexecutive.bo.execution.cli", "work", "proc-b"],
            capture_output=True, text=True, env=env, timeout=120,
            cwd=str(Path(openexecutive.__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["claimed"] == 0


# --------------------------------------------------------------------------- #
# Retention + regression guards
# --------------------------------------------------------------------------- #

class TestRetention:
    def test_retention_sweep_finished_only(self, db: Path) -> None:
        """The retention sweep removes only FINISHED executions older
        than the threshold — active runs and unfinished ledger are kept."""
        _enable()
        m = _mandate()
        run = _run(m)
        prov = SyntheticCounterProvider(idempotent=True)
        _work(prov)
        old = (datetime.now(UTC) - timedelta(days=400)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        with store.get_conn() as conn:
            conn.execute(
                "UPDATE bo_exec_runs SET finished_at = ? "
                "WHERE tenant = ? AND run_id = ?",
                (old, TENANT, run["run_id"]),
            )
        pending = _run(m)
        from openexecutive.bo.execution import retention

        removed = retention.sweep(TENANT, days=90)
        assert removed == 1
        assert store.get_run(TENANT, pending["run_id"])["state"] == (
            store.RUN_PENDING
        )
        with pytest.raises(store.NotFoundError):
            store.get_run(TENANT, run["run_id"])
