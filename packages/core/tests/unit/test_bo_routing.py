"""VAL3-01: observe-mode router — catalog, engine, observations, telemetry.

Probele cerute de mandat: replay determinist, lipsă evaluare/cost, prag
ratat, buget/regiune/provider interzis, fallback neeligibil, date stale,
izolare tenant, CAS concurent, pierdere receptor, nicio schimbare a
modelului real, BoBot cu LLM oprit (fără dependență de router).
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from openexecutive.bo import db as bo_db
from openexecutive.bo.routing import observe, serialize, store
from openexecutive.bo.routing.catalog import (
    CatalogEntry,
    CatalogValidationError,
    Cost,
    Quality,
)
from openexecutive.bo.routing.engine import Policy, TaskContext, recommend
from openexecutive.bo.settings import store as settings_store

from .bo_testkit import capture_audit, use_tmp_db

TENANT = "tenant-a"
NOW = datetime.now(UTC)
FRESH = (NOW - timedelta(days=5)).isoformat(timespec="milliseconds").replace(
    "+00:00", "Z"
)
STALE = (NOW - timedelta(days=400)).isoformat(timespec="milliseconds").replace(
    "+00:00", "Z"
)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return use_tmp_db(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    return capture_audit(monkeypatch)


@pytest.fixture(autouse=True)
def _stop_delivery_worker():
    """Never leak the background delivery thread across tests."""
    yield
    from openexecutive.bo.routing import delivery

    delivery.stop_worker()


def _entry(
    *,
    entry_id: str = "c1",
    provider: str = "anthropic",
    model_id: str = "claude-a",
    model_version: str | None = None,
    state: str = "ACTIVE",
    capabilities: tuple[str, ...] = ("analysis",),
    regions: tuple[str, ...] = ("eu",),
    cost: Cost | None = None,
    quality: Quality | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        entry_id=entry_id,
        provider=provider,
        model_id=model_id,
        model_version=model_version,
        state=state,
        capabilities=capabilities,
        regions=regions,
        cost=cost or Cost("3.00", "15.00", "USD", "2027-12-31"),
        quality=quality
        if quality is not None
        else Quality(0.9, "synthetic", "specialist", "setA", "v1", FRESH, 42),
        purpose="test",
        source="admin",
    )


def _policy(**kw: Any) -> Policy:
    base = dict(
        allowed_providers=None,
        allowed_regions=None,
        required_capabilities=frozenset(),
        min_quality=0.6,
        eval_max_age_days=90,
        max_estimated_cost=None,
        cost_currency=None,
    )
    base.update(kw)
    return Policy(**base)


CTX = TaskContext("specialist", input_tokens=1000, output_tokens=200)


# --------------------------------------------------------------------------- #
# Engine — determinism + filter order
# --------------------------------------------------------------------------- #

class TestEngine:
    def test_route_with_met_bar(self) -> None:
        d = recommend([_entry()], _policy(), CTX)
        assert d.decision == "ROUTE"
        assert d.met_bar is True
        assert d.recommendation == {
            "provider": "anthropic", "modelId": "claude-a",
            "modelVersion": None,
        }
        assert d.cost_estimate == {
            "amount": "0.006000", "currency": "USD", "validUntil": "2027-12-31"
        }

    def test_deterministic_replay(self) -> None:
        """Same inputs → byte-identical decision (the replay probe)."""
        catalog = [_entry(), _entry(entry_id="c2", model_id="claude-b")]
        args = (_policy(), CTX)
        now = datetime(2026, 9, 24, tzinfo=UTC)
        d1 = recommend(catalog, *args, now=now)
        d2 = recommend(list(reversed(catalog)), *args, now=now)
        assert json.dumps(d1.to_dict(), sort_keys=True) == json.dumps(
            d2.to_dict(), sort_keys=True
        )

    def test_tie_break_cost_then_identity(self) -> None:
        cheap = _entry(entry_id="b", model_id="m-b",
                       cost=Cost("1.00", "1.00", "USD", "2027-12-31"))
        dear = _entry(entry_id="a", model_id="m-a",
                      cost=Cost("9.00", "9.00", "USD", "2027-12-31"))
        d = recommend([dear, cheap], _policy(), CTX)
        assert d.recommendation["modelId"] == "m-b"

    def test_provider_denied(self) -> None:
        p = _policy(allowed_providers=frozenset({"other"}))
        d = recommend([_entry()], p, CTX)
        assert d.decision == "REFUSE"
        assert "PROVIDER_DENIED" in d.reasons

    def test_region_denied(self) -> None:
        p = _policy(allowed_regions=frozenset({"us"}))
        d = recommend([_entry(regions=("eu",))], p, CTX)
        assert "REGION_DENIED" in d.reasons
        assert d.decision == "REFUSE"

    def test_capability_missing(self) -> None:
        p = _policy(required_capabilities=frozenset({"vision"}))
        d = recommend([_entry()], p, CTX)
        assert "CAPABILITY_MISSING" in d.reasons

    def test_budget_exceeded(self) -> None:
        p = _policy(max_estimated_cost=Decimal("0.0001"), cost_currency="USD")
        d = recommend([_entry()], p, CTX)
        assert "BUDGET_EXCEEDED" in d.reasons
        assert d.decision == "REFUSE"

    def test_cost_missing_with_budget(self) -> None:
        no_cost = _entry(cost=Cost(None, None, None, None))
        p = _policy(max_estimated_cost=Decimal("1.00"), cost_currency="USD")
        d = recommend([no_cost], p, CTX)
        assert "COST_DATA_MISSING" in d.reasons

    def test_cost_missing_no_budget_still_eligible(self) -> None:
        """Unknown cost is not zero — without a cap it stays eligible."""
        no_cost = _entry(cost=Cost(None, None, None, None))
        d = recommend([no_cost], _policy(), CTX)
        assert d.decision == "ROUTE"
        assert d.cost_estimate is None

    def test_model_disabled(self) -> None:
        d = recommend([_entry(state="DISABLED")], _policy(), CTX)
        assert "MODEL_DISABLED" in d.reasons

    def test_eval_missing(self) -> None:
        no_eval = _entry(quality=Quality(
            None, "synthetic", "specialist", "setA", "v1", FRESH, 0))
        d = recommend([no_eval], _policy(), CTX)
        assert "EVAL_MISSING" in d.reasons
        assert d.decision == "REFUSE"

    def test_eval_task_mismatch(self) -> None:
        wrong = _entry(quality=Quality(
            0.95, "synthetic", "triage", "setA", "v1", FRESH, 10))
        d = recommend([wrong], _policy(), CTX)
        assert "EVAL_TASK_MISMATCH" in d.reasons

    def test_stale_evaluation(self) -> None:
        stale = _entry(quality=Quality(
            0.95, "synthetic", "specialist", "setA", "v1", STALE, 10))
        d = recommend([stale], _policy(eval_max_age_days=90), CTX)
        assert "STALE_EVALUATION" in d.reasons

    def test_quality_bar_unmet(self) -> None:
        weak = _entry(quality=Quality(
            0.4, "synthetic", "specialist", "setA", "v1", FRESH, 10))
        d = recommend([weak], _policy(min_quality=0.6), CTX)
        assert d.decision == "REFUSE"
        assert d.met_bar is False
        assert "QUALITY_BAR_UNMET" in d.reasons

    def test_fallback_does_not_relax_constraints(self) -> None:
        """A second-ranked candidate that fails filters stays eliminated —
        fallback never widens rights or budget."""
        ok = _entry(entry_id="ok")
        bad_region = _entry(entry_id="fb", model_id="claude-fb",
                            regions=("cn",))
        p = _policy(allowed_regions=frozenset({"eu"}))
        d = recommend([ok, bad_region], p, CTX)
        assert d.decision == "ROUTE"
        fb = [c for c in d.candidates if c.entry.entry_id == "fb"][0]
        assert fb.eligible is False and fb.reason == "REGION_DENIED"

    def test_catalog_empty(self) -> None:
        d = recommend([], _policy(), CTX)
        assert d.decision == "REFUSE"
        assert d.reasons == ["CATALOG_EMPTY"]

    def test_filters_run_before_scoring(self) -> None:
        """A high-scoring but provider-denied candidate must not win."""
        denied = _entry(quality=Quality(
            0.99, "synthetic", "specialist", "setA", "v1", FRESH, 10))
        ok = _entry(entry_id="ok", model_id="claude-ok",
                    provider="allowed-p")
        p = _policy(allowed_providers=frozenset({"allowed-p"}))
        d = recommend([denied, ok], p, CTX)
        assert d.recommendation["provider"] == "allowed-p"


# --------------------------------------------------------------------------- #
# Catalog store — CRUD, CAS, isolation
# --------------------------------------------------------------------------- #

def _fields(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "provider": "anthropic",
        "model_id": "claude-x",
        "model_version": None,
        "state": "ACTIVE",
        "capabilities": ["analysis"],
        "regions": ["eu"],
        "cost": {
            "input_per_million": "3.00",
            "output_per_million": "15.00",
            "currency": "USD",
            "valid_until": "2027-12-31",
        },
        "quality": {
            "score": 0.9, "methodology": "synthetic", "task_kind": "specialist",
            "eval_set_ref": "setA", "eval_set_version": "v1",
            "observed_at": FRESH, "sample_count": 42,
        },
        "purpose": "chat",
        "source": "admin",
    }
    base.update(kw)
    return base


class TestCatalogStore:
    def test_create_and_list(self, db: Path, audit: list[dict]) -> None:
        e = store.create_entry(TENANT, _fields(), actor="admin@t")
        assert e.version == 1
        assert store.catalog_version(TENANT) == 1
        listed = store.list_catalog(TENANT)
        assert [x.entry_id for x in listed] == [e.entry_id]
        assert listed[0].quality is not None and listed[0].quality.score == 0.9
        events = [a for a in audit if a["event_type"] == "bo_catalog_change"]
        assert events and events[0]["details"]["action"] == "create"

    def test_update_cas_conflict(self, db: Path) -> None:
        e = store.create_entry(TENANT, _fields(), actor="admin@t")
        store.update_entry(TENANT, e.entry_id, _fields(state="DISABLED"),
                           expected_version=1, actor="admin@t")
        with pytest.raises(store.ConflictError):
            store.update_entry(TENANT, e.entry_id, _fields(),
                               expected_version=1, actor="admin@t")

    def test_duplicate_identity_rejected(self, db: Path) -> None:
        store.create_entry(TENANT, _fields(), actor="admin@t")
        with pytest.raises(store.DuplicateEntryError):
            store.create_entry(TENANT, _fields(), actor="admin@t")

    def test_tenant_isolation(self, db: Path) -> None:
        store.create_entry("tenant-a", _fields(), actor="a@t")
        assert store.list_catalog("tenant-b") == []
        assert store.catalog_version("tenant-b") == 0
        with pytest.raises(store.NotFoundError):
            e = store.list_catalog("tenant-a")[0]
            store.get_entry("tenant-b", e.entry_id)

    def test_invalid_entry_rejected(self, db: Path) -> None:
        with pytest.raises(CatalogValidationError):
            store.create_entry(
                TENANT, _fields(provider="bad provider@x"), actor="a@t"
            )
        with pytest.raises(CatalogValidationError):
            store.create_entry(
                TENANT, _fields(quality={"score": 1.5, "methodology": "m",
                                         "task_kind": "t", "eval_set_ref": "r",
                                         "eval_set_version": "v",
                                         "observed_at": FRESH,
                                         "sample_count": 1}),
                actor="a@t",
            )


# --------------------------------------------------------------------------- #
# Observe hook — settings gate, persistence, telemetry, receiver loss
# --------------------------------------------------------------------------- #

class TestObserve:
    def _enable(self, db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        settings_store.set_value(
            TENANT, "bo.router.observe_enabled", True,
            expected_version=0, actor="admin@t", db_path=db,
        )

    def test_disabled_by_default_no_write(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        out = observe.observe_call(
            model="claude-sonnet-5", actor="specialist",
            counts={"input_tokens": 10, "output_tokens": 5},
        )
        assert out is None
        assert store.list_observations(TENANT) == []

    def test_observation_persisted(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._enable(db, monkeypatch)
        store.create_entry(TENANT, _fields(), actor="admin@t", db_path=db)
        out = observe.observe_call(
            model="claude-sonnet-5", actor="specialist",
            counts={"input_tokens": 1000, "output_tokens": 200,
                    "cost_usd": 0.006},
            turn_id="turn-1",
        )
        assert out is not None and out["decision"] == "ROUTE"
        rows = store.list_observations(TENANT)
        assert len(rows) == 1
        row = rows[0]
        assert row["correlation_id"] == "turn-1"
        assert row["task_kind"] == "specialist"
        assert row["actual_route"]["modelId"] == "claude-sonnet-5"
        assert row["actual_route"]["provider"] == "anthropic"
        assert row["met_bar"] is True
        assert row["measured"]["inputTokens"] == 1000
        assert row["billed"]["evidenceRef"] == "provider:usage.cost"
        # Telemetry disabled by default → persisted but undelivered (0).
        assert row["delivered"] == 0
        # REM-01: the stable eventId is persisted with the envelope.
        assert row["event_id"] and row["event_id"].startswith("evt_")

    def test_actual_route_unchanged_regardless_of_recommendation(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The observed REFUSE never rewrites the real route — the probe for
        'nicio schimbare a modelului real'."""
        self._enable(db, monkeypatch)
        store.create_entry(
            TENANT, _fields(provider="other-vendor"), actor="a@t", db_path=db)
        settings_store.set_value(
            TENANT, "bo.router.allowed_providers", "anthropic",
            expected_version=0, actor="admin@t", db_path=db)
        out = observe.observe_call(
            model="claude-sonnet-5", actor="specialist", counts=None)
        assert out is not None
        row = store.list_observations(TENANT)[0]
        assert row["decision"] == "REFUSE"
        assert row["actual_route"]["modelId"] == "claude-sonnet-5"
        assert "PROVIDER_DENIED" in row["reasons"]

    def test_receiver_loss_persists_and_flushes(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._enable(db, monkeypatch)
        store.create_entry(TENANT, _fields(), actor="a@t", db_path=db)

        from openexecutive.bo.telemetry import adapter as tel

        class Down:
            def send(self, event: dict) -> None:
                raise ConnectionError("guardian down")

        # The hook itself performs NO send — the row is persisted pending.
        monkeypatch.setattr(
            tel, "_adapter", tel.TelemetryAdapter(enabled=True, transport=Down())
        )
        observe.observe_call(
            model="claude-x", actor="specialist", counts=None)
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 0

        # First delivery attempt fails → error recorded, still pending.
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 0 and res["failed"] == 1
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 0
        assert "guardian down" in (row["delivery_error"] or "")

        sent: list[dict] = []

        class Up:
            def send(self, event: dict) -> dict:
                sent.append(event)
                return {"status": "RECEIVED"}

        monkeypatch.setattr(
            tel, "_adapter", tel.TelemetryAdapter(enabled=True, transport=Up())
        )
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 1 and res["failed"] == 0
        assert sent and sent[0]["schemaVersion"] == "bo.model-observation.v1"
        assert sent[0]["product"] == "BOAgents"
        assert sent[0]["tenantRef"] == TENANT
        assert store.list_observations(TENANT)[0]["delivered"] == 1


# --------------------------------------------------------------------------- #
# REM-01 — durable outbox: stable identity at retry, leased concurrent
# delivery, no network in the hook, dead-letter visibility
# --------------------------------------------------------------------------- #

class TestDeliveryOutbox:
    def _enable(self, db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        settings_store.set_value(
            TENANT, "bo.router.observe_enabled", True,
            expected_version=0, actor="admin@t", db_path=db,
        )

    def _adapter(self, monkeypatch: pytest.MonkeyPatch, transport: Any):
        from openexecutive.bo.telemetry import adapter as tel

        ad = tel.TelemetryAdapter(enabled=True, transport=transport)
        monkeypatch.setattr(tel, "_adapter", ad)
        return ad

    def test_hook_never_touches_network(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REM-01/V3-R2: a transport that would block for the HTTP timeout is
        never invoked inside observe_call — zero Guardian I/O in the hook."""
        self._enable(db, monkeypatch)
        calls: list[dict] = []

        class SlowReceiver:
            def send(self, event: dict) -> None:
                calls.append(event)
                import time

                time.sleep(30)  # would blow up any synchronous call path

        self._adapter(monkeypatch, SlowReceiver())
        import time

        t0 = time.monotonic()
        out = observe.observe_call(
            model="claude-x", actor="specialist", counts=None)
        elapsed = time.monotonic() - t0
        assert out is not None
        assert calls == []          # transport never invoked by the hook
        assert elapsed < 5          # no HTTP timeout leaks into the call path
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 0

    def test_ack_lost_retry_sends_identical_envelope(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REM-01/V3-R1: receiver GOT the event but the ACK was lost — the
        retry re-sends byte-identical bytes (same eventId), which is what
        lets Guardian answer DUPLICATE instead of storing it twice."""
        self._enable(db, monkeypatch)
        received: list[str] = []

        class AckLost:
            def send(self, event: dict) -> None:
                received.append(json.dumps(event, sort_keys=True))
                raise TimeoutError("ack lost after write")

        self._adapter(monkeypatch, AckLost())
        observe.observe_call(model="m1", actor="specialist", counts=None)

        res = observe.flush_pending(TENANT)
        assert res["failed"] == 1 and received
        assert store.list_observations(TENANT)[0]["delivered"] == 0

        # Retry — identical bytes on the wire.
        res = observe.flush_pending(TENANT)
        assert res["failed"] == 1
        assert len(received) == 2
        assert received[0] == received[1]
        assert json.loads(received[0])["eventId"] == \
            store.list_observations(TENANT)[0]["event_id"]

    def test_restart_preserves_envelope_identity(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Restart between receive and confirm: a NEW adapter instance must
        re-send the persisted envelope unchanged — identity is read from the
        outbox row, not regenerated."""
        self._enable(db, monkeypatch)

        class Down:
            def send(self, event: dict) -> None:
                raise ConnectionError("down")

        self._adapter(monkeypatch, Down())
        observe.observe_call(model="m1", actor="specialist", counts=None)
        before = store.list_observations(TENANT)[0]["event_id"]
        observe.flush_pending(TENANT)

        sent: list[dict] = []

        class Up:
            def send(self, event: dict) -> dict:
                sent.append(event)
                return {"status": "RECEIVED"}

        # Fresh adapter instance = "process restarted".
        self._adapter(monkeypatch, Up())
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 1
        assert sent[0]["eventId"] == before

    def test_new_observation_new_event_id(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely new emission gets a NEW eventId even when the body
        looks identical — dedup never collapses real observations."""
        self._enable(db, monkeypatch)
        observe.observe_call(model="m", actor="specialist", counts=None)
        observe.observe_call(model="m", actor="specialist", counts=None)
        rows = store.list_observations(TENANT)
        assert len(rows) == 2
        assert rows[0]["event_id"] != rows[1]["event_id"]

    def test_duplicate_ack_counts_as_delivered(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RECEIVED/DUPLICATE both mean the receiver holds the event."""
        self._enable(db, monkeypatch)

        class DupAck:
            def send(self, event: dict) -> dict:
                return {"status": "DUPLICATE", "eventId": event["eventId"]}

        self._adapter(monkeypatch, DupAck())
        observe.observe_call(model="m", actor="specialist", counts=None)
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 1 and res["failed"] == 0
        assert store.list_observations(TENANT)[0]["delivered"] == 1

    def test_concurrent_flush_no_duplicate_sends(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two flush workers racing on the same rows must not double-send:
        the lease claim under BEGIN IMMEDIATE gives each row to one worker."""
        import threading

        self._enable(db, monkeypatch)
        sent: list[str] = []
        lock = threading.Lock()

        class Up:
            def send(self, event: dict) -> dict:
                with lock:
                    sent.append(event["eventId"])
                return {"status": "RECEIVED"}

        adapter = self._adapter(monkeypatch, Up())
        for _ in range(6):
            observe.observe_call(model="m", actor="specialist", counts=None)

        results: list[dict] = []

        def worker() -> None:
            from openexecutive.bo.routing import delivery

            results.append(delivery.deliver_pending(TENANT, adapter=adapter))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(sent) == 6                      # every event sent
        assert len(set(sent)) == 6                 # none sent twice
        assert sum(r["sent"] for r in results) == 6
        assert all(
            o["delivered"] == 1 for o in store.list_observations(TENANT)
        )

    def test_attempt_cap_marks_dead_and_visible(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Past delivery_max_attempts the envelope is dead-lettered —
        visible in status/UI, never silently dropped or retried forever."""
        self._enable(db, monkeypatch)
        settings_store.set_value(
            TENANT, "bo.router.delivery_max_attempts", 2,
            expected_version=0, actor="admin@t", db_path=db,
        )

        class Down:
            def send(self, event: dict) -> None:
                raise ConnectionError("still down")

        self._adapter(monkeypatch, Down())
        observe.observe_call(model="m", actor="specialist", counts=None)

        for _ in range(3):
            observe.flush_pending(TENANT)
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 2
        assert row["delivery_error"] == "attempt cap reached"
        stats = store.observation_stats(TENANT)
        assert stats["dead_delivery"] == 1
        assert store.outbox_stats(TENANT)["outbox_dead"] == 1
        # Dead rows are excluded from further claims.
        res = observe.flush_pending(TENANT)
        assert res["claimed"] == 0

    def test_catalog_sync_persisted_and_retried(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Catalog sync goes through the same durable outbox — persisted
        before send, retried identically, coalesced while pending."""
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        store.create_entry(TENANT, _fields(), actor="a@t", db_path=db)

        class Down:
            def send(self, event: dict) -> None:
                raise ConnectionError("down")

        self._adapter(monkeypatch, Down())
        observe.emit_catalog_sync(TENANT, db_path=db)
        observe.emit_catalog_sync(TENANT, db_path=db)   # coalesces
        stats = store.outbox_stats(TENANT)
        assert stats["outbox_pending"] == 1

        sent: list[dict] = []

        class Up:
            def send(self, event: dict) -> dict:
                sent.append(event)
                return {"status": "RECEIVED"}

        self._adapter(monkeypatch, Up())
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 1
        assert sent[0]["models"]
        assert "routing" not in sent[0]

    def test_outbox_tenant_isolation(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        observe.emit_catalog_sync(TENANT, db_path=db)
        observe.emit_catalog_sync("tenant-b", db_path=db)
        stats_a = store.outbox_stats(TENANT)
        stats_b = store.outbox_stats("tenant-b")
        assert stats_a["outbox_pending"] == 1
        assert stats_b["outbox_pending"] == 1
        assert store.outbox_stats("tenant-c")["outbox_pending"] == 0

    def test_disabled_adapter_keeps_pending(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telemetry disabled → flush marks the attempt failed (visible),
        envelope stays pending for a later retry."""
        self._enable(db, monkeypatch)
        from openexecutive.bo.telemetry import adapter as tel

        monkeypatch.setattr(tel, "_adapter", tel.TelemetryAdapter(enabled=False))
        observe.observe_call(model="m", actor="specialist", counts=None)
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 0 and res["failed"] == 1
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 0
        assert row["delivery_error"] == "telemetry disabled"

    def test_retention_sweep(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._enable(db, monkeypatch)
        observe.observe_call(model="m", actor="triage", counts=None)
        deleted = store.sweep_observations(TENANT, 0 + 1)
        assert deleted == 0  # fresh row survives
        assert store.sweep_observations(TENANT, 1) == 0
        # A row backdated beyond retention is removed.
        with bo_db.get_conn() as conn:
            conn.execute(
                "UPDATE bo_route_observations SET occurred_at = ?",
                ("2020-01-01T00:00:00Z",),
            )
        assert store.sweep_observations(TENANT, 1) == 1


# --------------------------------------------------------------------------- #
# S-01 — per-tenant worker cadence: interval re-read on every cycle,
# 0 = manual only, a slow tenant never delays a fast one
# --------------------------------------------------------------------------- #

class TestPerTenantWorker:
    def _adapter(self, monkeypatch: pytest.MonkeyPatch):
        from openexecutive.bo.telemetry import adapter as tel

        ad = tel.TelemetryAdapter(enabled=True, transport=tel.BufferedTransport())
        monkeypatch.setattr(tel, "_adapter", ad)
        return ad

    def _pending(self, tenant: str, ref: str, db: Path) -> None:
        store.enqueue_outbox(
            tenant, "models", ref,
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": f"evt_{tenant}_{ref}", "tenantRef": tenant},
            db_path=db,
        )

    def _set_interval(self, tenant: str, seconds: int, db: Path) -> None:
        with bo_db.get_conn(db) as conn:
            row = conn.execute(
                "SELECT version FROM bo_settings WHERE tenant = ? AND key = ?",
                (tenant, "bo.router.delivery_interval_s"),
            ).fetchone()
        settings_store.set_value(
            tenant, "bo.router.delivery_interval_s", seconds,
            expected_version=0 if row is None else int(row["version"]),
            actor="admin@t", db_path=db,
        )

    def test_interval_zero_is_manual_even_with_active_tenant(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tenant A at interval 0 must NOT be drained just because tenant B
        (interval 30) has an active schedule — the per-tenant read replaces
        the old global max()."""
        from openexecutive.bo.routing import delivery

        adapter = self._adapter(monkeypatch)
        self._pending("tenant-manual", "m1", db)
        self._pending("tenant-auto", "a1", db)
        self._set_interval("tenant-manual", 0, db)
        self._set_interval("tenant-auto", 30, db)

        due: dict[str, float] = {}
        now = 1000.0
        delivery._worker_cycle(now, due, adapter, db)
        assert "tenant-manual" not in due          # never even scheduled
        assert due["tenant-auto"] == now + 30
        delivery._worker_cycle(now + 31, due, adapter, db)  # past due
        assert store.outbox_stats("tenant-manual", db_path=db)["outbox_pending"] == 1
        assert store.outbox_stats("tenant-auto", db_path=db)["outbox_pending"] == 0

        # Manual flush is still the drain for the interval-0 tenant.
        res = observe.flush_pending("tenant-manual", db_path=db)
        assert res["sent"] == 1
        assert store.outbox_stats("tenant-manual", db_path=db)["outbox_pending"] == 0

    def test_fast_tenant_not_delayed_by_slow_tenant(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 3600s tenant cannot push a 5s tenant's wake: the sleep is the
        MINIMUM due time, never the maximum interval."""
        from openexecutive.bo.routing import delivery

        adapter = self._adapter(monkeypatch)
        self._pending("tenant-fast", "f", db)
        self._pending("tenant-slow", "s", db)
        self._set_interval("tenant-fast", 5, db)
        self._set_interval("tenant-slow", 3600, db)

        due: dict[str, float] = {}
        wait = delivery._worker_cycle(0.0, due, adapter, db)
        assert wait == 5
        assert due["tenant-slow"] == 3600
        delivery._worker_cycle(5.0, due, adapter, db)
        assert store.outbox_stats("tenant-fast", db_path=db)["outbox_pending"] == 0
        assert store.outbox_stats("tenant-slow", db_path=db)["outbox_pending"] == 1
        # the slow tenant's own due time was not consumed by the fast drain
        assert due["tenant-slow"] == 3600

    def test_live_interval_transitions_without_restart(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cycle-level live re-read: 30→0 drops the pending schedule, 0→10
        reschedules fresh — no process restart, no instant overdue fire."""
        from openexecutive.bo.routing import delivery

        adapter = self._adapter(monkeypatch)
        self._pending("tenant-live", "l1", db)
        self._set_interval("tenant-live", 30, db)

        due: dict[str, float] = {}
        delivery._worker_cycle(0.0, due, adapter, db)
        assert due["tenant-live"] == 30

        # 30 → 0 while the schedule is pending: the tenant is excluded and
        # the stale due entry is dropped — its envelope stays pending.
        self._set_interval("tenant-live", 0, db)
        delivery._worker_cycle(40.0, due, adapter, db)   # would have been due
        assert "tenant-live" not in due
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 1

        # 0 → 10 re-enables automatic delivery with a FRESH countdown —
        # the envelope does not fire instantly on the stale schedule.
        self._set_interval("tenant-live", 10, db)
        delivery._worker_cycle(50.0, due, adapter, db)
        assert due["tenant-live"] == 60.0
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 1
        delivery._worker_cycle(61.0, due, adapter, db)
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 0

    def test_disabled_telemetry_tenant_skipped_by_cycle(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Administered bo.telemetry.enabled=false is honored per tick even
        though the bootstrap adapter is enabled (S-02 wiring into S-01)."""
        from openexecutive.bo.routing import delivery

        adapter = self._adapter(monkeypatch)   # bootstrap enabled
        self._pending("tenant-off", "x", db)
        self._set_interval("tenant-off", 30, db)
        settings_store.set_value(
            "tenant-off", "bo.telemetry.enabled", False,
            expected_version=0, actor="admin@t", db_path=db,
        )
        due: dict[str, float] = {}
        delivery._worker_cycle(0.0, due, adapter, db)
        delivery._worker_cycle(40.0, due, adapter, db)   # past the 30s mark
        assert "tenant-off" not in due
        assert store.outbox_stats("tenant-off", db_path=db)["outbox_pending"] == 1

    def test_ensure_worker_ignores_manual_only_tenants(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openexecutive.bo.routing import delivery

        self._adapter(monkeypatch)
        self._pending("tenant-manual", "m", db)
        self._set_interval("tenant-manual", 0, db)
        assert delivery.ensure_worker(db_path=db) is False
        assert delivery._worker_thread is None
        # …but one auto tenant among manual ones does start it.
        self._pending("tenant-auto", "a", db)
        self._set_interval("tenant-auto", 30, db)
        assert delivery.ensure_worker(db_path=db) is True

    def test_worker_thread_live_transition(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real daemon thread: interval-1s tenant drains automatically,
        then a live switch to 0 stops auto-delivery without a restart."""
        from openexecutive.bo.routing import delivery

        self._adapter(monkeypatch)
        self._set_interval("tenant-live", 1, db)
        self._pending("tenant-live", "t1", db)
        assert delivery.ensure_worker(db_path=db) is True

        deadline = time.time() + 6
        while time.time() < deadline:
            if store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 0:
                break
            time.sleep(0.05)
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 0

        self._set_interval("tenant-live", 0, db)
        self._pending("tenant-live", "t2", db)
        time.sleep(2.5)  # several ticks at the 1s cadence would have fired
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 1


# --------------------------------------------------------------------------- #
# Wire contract — serializer output validates against bo.model-observation.v1
# --------------------------------------------------------------------------- #

class TestWireContract:
    SCHEMA = json.loads(
        (Path(__file__).parent / "fixtures"
         / "bo.model-observation.v1.schema.json").read_text()
    )

    def _validate(self, doc: dict[str, Any]) -> None:
        import jsonschema

        jsonschema.validate(doc, self.SCHEMA)

    def test_routing_doc_validates(self) -> None:
        body = serialize.routing_body(
            correlation_id="turn-1", policy_version="pol_x",
            catalog_version="cat_v3", task_kind="specialist",
            recommendation={"provider": "anthropic", "modelId": "claude-a",
                            "modelVersion": None},
            actual_route={"provider": "anthropic", "modelId": "claude-b",
                          "modelVersion": None},
            met_bar=True, reasons=[],
            cost_estimate={"amount": "0.006000", "currency": "USD",
                           "validUntil": "2027-12-31"},
            measured={"inputTokens": 1000, "outputTokens": 200,
                      "requests": 1},
            billed={"amount": "0.006", "currency": "USD",
                    "validUntil": "2026-09-24",
                    "evidenceRef": "provider:usage.cost"},
        )
        doc = {
            "schemaVersion": "bo.model-observation.v1",
            "eventId": "evt_1", "producerId": "boagents",
            "product": "BOAgents", "installationId": "inst-1",
            "tenantRef": "tenant-a", "observedAt": FRESH,
            **body,
        }
        self._validate(doc)

    def test_refuse_doc_validates(self) -> None:
        body = serialize.routing_body(
            correlation_id="turn-2", policy_version="pol_x",
            catalog_version="cat_v0", task_kind="specialist",
            recommendation=None, actual_route=None,
            met_bar=False, reasons=["QUALITY_BAR_UNMET"],
            cost_estimate=None, measured=None, billed=None,
        )
        doc = {
            "schemaVersion": "bo.model-observation.v1",
            "eventId": "evt_2", "producerId": "boagents",
            "product": "BOAgents", "installationId": "inst-1",
            "tenantRef": "tenant-a", "observedAt": FRESH,
            **body,
        }
        self._validate(doc)

    def test_models_doc_validates(self) -> None:
        e = _entry()
        body = serialize.models_body(
            [e], owner_ref="tenant:tenant-a", last_seen=FRESH,
            sync_id="sync_1", complete=True)
        doc = {
            "schemaVersion": "bo.model-observation.v1",
            "eventId": "evt_3", "producerId": "boagents",
            "product": "BOAgents", "installationId": "inst-1",
            "tenantRef": "tenant-a", "observedAt": FRESH,
            **body,
        }
        self._validate(doc)
        model = doc["models"][0]
        assert model["quality"]["score"] == 0.9
        assert model["modelVersion"] is None  # unknown stays explicit


# --------------------------------------------------------------------------- #
# BoBots must not gain an LLM/router dependency
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# HTTP surface — RBAC, CAS over the wire, tenant isolation
# --------------------------------------------------------------------------- #

class TestRoutes:
    ADMIN = {"x-caller-email": "admin@test", "x-caller-proxy-secret": "test-proxy-only"}
    VIEWER = {"x-caller-email": "viewer@test", "x-caller-proxy-secret": "test-proxy-only"}

    @pytest.fixture()
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from openexecutive.api.routes import bo as bo_route

        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
        monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@test")
        monkeypatch.setenv("BACKEND_PROXY_SECRET", "test-proxy-only")
        app = FastAPI()
        app.include_router(bo_route.router)
        bo_route.register_error_handlers(app)
        return TestClient(app)

    def test_catalog_requires_admin_write(self, client) -> None:
        resp = client.post("/bo/routing/catalog", headers=self.VIEWER,
                           json=_fields())
        assert resp.status_code == 403
        resp = client.get("/bo/routing/catalog", headers=self.VIEWER)
        assert resp.status_code == 200
        assert resp.json()["entries"] == []

    def test_catalog_crud_and_cas_over_http(self, client) -> None:
        resp = client.post("/bo/routing/catalog", headers=self.ADMIN,
                           json=_fields())
        assert resp.status_code == 201, resp.text
        entry = resp.json()["entry"]
        assert entry["version"] == 1

        # stale CAS → 409
        resp = client.put(
            f"/bo/routing/catalog/{entry['entry_id']}", headers=self.ADMIN,
            json={**_fields(state="DISABLED"), "expected_version": 99})
        assert resp.status_code == 409

        resp = client.put(
            f"/bo/routing/catalog/{entry['entry_id']}", headers=self.ADMIN,
            json={**_fields(state="DISABLED"), "expected_version": 1})
        assert resp.status_code == 200
        assert resp.json()["entry"]["state"] == "DISABLED"
        assert resp.json()["entry"]["version"] == 2

    def test_catalog_validation_422(self, client) -> None:
        resp = client.post("/bo/routing/catalog", headers=self.ADMIN,
                           json=_fields(provider="bad provider"))
        assert resp.status_code == 422

    def test_status_and_observations_shape(self, client) -> None:
        resp = client.get("/bo/routing/status", headers=self.VIEWER)
        assert resp.status_code == 200
        body = resp.json()
        assert body["observe_enabled"] is False
        assert body["mode"] == "observare"
        resp = client.get("/bo/routing/observations", headers=self.VIEWER)
        assert resp.status_code == 200
        assert resp.json()["observations"] == []

    def test_settings_new_keys_roundtrip(self, client) -> None:
        resp = client.put(
            "/bo/settings/bo.router.observe_enabled", headers=self.ADMIN,
            json={"value": True, "expected_version": 0})
        assert resp.status_code == 200, resp.text
        resp = client.get("/bo/routing/status", headers=self.VIEWER)
        assert resp.json()["observe_enabled"] is True

        # invalid CSV rejected
        resp = client.put(
            "/bo/settings/bo.router.allowed_providers", headers=self.ADMIN,
            json={"value": "bad provider x", "expected_version": 0})
        assert resp.status_code == 422

        # invalid cost cap rejected
        resp = client.put(
            "/bo/settings/bo.router.max_estimated_cost", headers=self.ADMIN,
            json={"value": "abc", "expected_version": 0})
        assert resp.status_code == 422
        resp = client.put(
            "/bo/settings/bo.router.max_estimated_cost", headers=self.ADMIN,
            json={"value": "0.05 USD", "expected_version": 0})
        assert resp.status_code == 200


def test_bobots_have_no_router_dependency() -> None:
    """Static check: no bo.bots module imports bo.routing or providers."""
    import ast

    bots_dir = (
        Path(__file__).parents[2] / "openexecutive" / "bo" / "bots"
    )
    for src in bots_dir.glob("*.py"):
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert "bo.routing" not in n and "providers" not in n, (
                    f"{src.name} imports {n} — BoBots stay LLM-free"
                )
