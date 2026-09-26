"""Adverse regressions for BO-SOL-02 audit, synthetic isolated database."""
from decimal import Decimal
import pytest
from starlette.requests import Request
from openexecutive.bo import identity
from openexecutive.bo.execution import engine, store, guardian
from openexecutive.bo.execution.mandate import MandateValidationError
from openexecutive.bo.settings import store as settings
from .test_bo_execution import db, audit, _enable, _mandate, _run, _step, _work, TENANT
from openexecutive.bo.execution.synth import SyntheticCounterProvider


def test_f1_service_cannot_delegate_admin(monkeypatch):
    monkeypatch.setenv('BO_ADMIN_EMAILS', 'admin@example.invalid')
    monkeypatch.setattr(identity, '_is_principal_email', lambda _: False)
    req = Request({'type': 'http', 'headers': [(b'x-api-key', b'service'), (b'x-caller-email', b'admin@example.invalid')], 'query_string': b''})
    with pytest.raises(identity.UnauthenticatedError):
        identity.resolve_identity(req)


def test_f2_ancestor_guardian_is_checked(db, monkeypatch):
    _enable()
    parent = _mandate(guardian_ref='revoked-parent')
    child = _mandate(parent=parent, guardian_ref='other-child')
    def check(tenant, mandate, **kw):
        if mandate.guardian_ref == 'revoked-parent':
            raise guardian.GuardianDeniedError('revoked', 'revoked ancestor')
    monkeypatch.setattr(guardian, 'assert_effect_authorized', check)
    run = _run(child)
    _work(SyntheticCounterProvider(idempotent=True))
    assert store.get_run(TENANT, run['run_id'])['state'] == store.RUN_FAILED


def test_f3_disabled_before_claim(db):
    _enable()
    run = _run(_mandate())
    settings.set_value(TENANT, 'bo.exec.enabled', False, expected_version=1, actor='admin')
    assert _work(SyntheticCounterProvider(idempotent=True))['claimed'] == 0
    assert store.get_run(TENANT, run['run_id'])['state'] == store.RUN_PENDING


def test_f4_resume_reacquires_capacity(db):
    _enable()
    m = _mandate(concurrency_limit=1)
    run = _run(m)
    store.transition_run(TENANT, run['run_id'], store.RUN_UNKNOWN, clear_lease=True, reservation_state=store.RES_RELEASED)
    _run(m)
    with pytest.raises(store.BudgetExceededError):
        engine.resume_run(TENANT, run['run_id'], actor='admin')


def test_f5_reconcile_invalidates_old_fence(db):
    _enable()
    run = _run(_mandate())
    entry = store.get_or_create_intent(TENANT, run, 0, provider='synth', payload={})
    claimed = store.claim_ledger_entry(TENANT, entry['entry_id'], worker_id='old', lease_s=60)
    store.mark_ledger_status(TENANT, entry['entry_id'], store.LED_SUCCEEDED, receipt_ref='receipt-safe')
    assert not store.finalize_ledger_entry(TENANT, entry['entry_id'], status=store.LED_UNKNOWN, fence_version=claimed['fence_version'])
    assert store.get_ledger_entry(TENANT, entry['entry_id'])['receipt_ref'] == 'receipt-safe'


def test_f6_children_share_parent_capacity(db):
    _enable()
    parent = _mandate(budget_limit='1', concurrency_limit=1)
    children = [_mandate(parent=parent, budget_limit='1', concurrency_limit=1) for _ in range(2)]
    _run(children[0])
    with pytest.raises(store.BudgetExceededError):
        _run(children[1])


@pytest.mark.parametrize('flag,state', [('cancel_requested', store.RUN_CANCELLED), ('pause_requested', store.RUN_PAUSED)])
def test_f7_pending_control(db, flag, state):
    _enable()
    run = _run(_mandate())
    result = store.request_flag(TENANT, run['run_id'], flag, actor='admin')
    assert result['state'] == state
    if state == store.RUN_PAUSED:
        assert engine.resume_run(TENANT, run['run_id'], actor='admin')['state'] == store.RUN_PENDING
    else:
        assert store.reservation_for(TENANT, run['run_id'])['state'] == store.RES_RELEASED


def test_f8_admin_step_cap(db):
    _enable()
    settings.set_value(TENANT, 'bo.exec.max_steps', 1, expected_version=0, actor='admin')
    with pytest.raises(MandateValidationError):
        _run(_mandate(), [_step(), _step()])


def test_f9_authority_revoked_local(db):
    from openexecutive.api.routes.bo import run_authority
    _enable()
    m = _mandate()
    run = _run(m)
    store.revoke_mandate(TENANT, m.mandate_id, reason='test', actor='admin')
    ident = identity.Identity('admin', TENANT, 'admin', False, 'dev')
    assert run_authority(run['run_id'], ident)['authorized'] is False
