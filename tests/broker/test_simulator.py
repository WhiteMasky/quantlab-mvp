"""Deterministic simulator fault tests, not broker integration tests."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quantlab.broker import BrokerSafetyError, SimulatedBroker
from quantlab.models import AccountSnapshot, OrderIntent, Position

D = Decimal
AT = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)


def intent(key='one', **changes):
    return replace(OrderIntent(key, 'client-' + key, 'SPY', 'BUY', 10, AT, D('100')), **changes)


def account(quantities=None, **changes):
    positions = {s: Position(s, q, D('100'), D('101')) for s, q in (quantities or {}).items()}
    return replace(AccountSnapshot(D('10000'), D('11000'), positions, AT + timedelta(hours=1)), **changes)


@pytest.fixture
def broker(tmp_path):
    result = SimulatedBroker(tmp_path / 'sim.sqlite')
    yield result
    result.close()


def apply(broker, order, quantity=10, fill_id='fill-1', seconds=1, **changes):
    params = dict(quantity=quantity, price=D('100'), commission=D('1'),
                  at=AT + timedelta(seconds=seconds), fill_id=fill_id)
    params.update(changes)
    return broker.fill(order.order_id, **params)


def test_public_initial_state_and_no_implicit_fill(broker):
    assert broker.status()['reconciled'] is True
    assert broker.status()['environment'] == 'SIMULATED'
    assert broker.status()['live_enabled'] is False
    order = broker.submit(intent())
    assert order.status == 'SUBMITTED'
    assert broker.fills() == []
    assert broker.submit(intent()) == order
    assert len(broker.orders()) == 1


@pytest.mark.parametrize('change', [dict(quantity=11), dict(symbol='QQQ'),
                                    dict(intent_id='another'), dict(client_order_id='another')])
def test_id_payload_conflicts_are_not_new_orders(broker, change):
    broker.submit(intent())
    with pytest.raises(BrokerSafetyError, match='ID reuse'):
        broker.submit(intent(**change))
    assert len(broker.orders()) == 1


def test_paper_intent_rejected_by_offline_simulator(broker):
    with pytest.raises(BrokerSafetyError):
        broker.submit(intent(mode='IBKR_PAPER'))
    assert broker.orders() == []


@pytest.mark.parametrize('fault', ['timeout', 'disconnect'])
def test_unknown_submit_durable_dedup_and_no_blind_retry(tmp_path, fault):
    path = tmp_path / 'durable.sqlite'
    first = SimulatedBroker(path)
    first.inject_submit_fault(fault)
    order = first.submit(intent())
    assert order.status == 'UNKNOWN'
    assert first.submit(intent()).order_id == order.order_id
    with pytest.raises(BrokerSafetyError):
        first.submit(intent('second'))
    assert not first.reconcile()['reconciled']
    first.close()

    restarted = SimulatedBroker(path)
    try:
        assert not restarted.status()['can_submit']
        assert restarted.submit(intent()).status == 'UNKNOWN'
        restarted.reconnect()
        assert not restarted.reconcile()['reconciled']
        with pytest.raises(BrokerSafetyError):
            restarted.cancel(order.order_id, authorized=True)
        restarted.on_order_status(order.order_id, 'SUBMITTED', 0, sequence=1)
        assert restarted.reconcile(account())['reconciled']
        assert restarted.submit(intent()).order_id == order.order_id
        assert len(restarted.orders()) == 1
    finally:
        restarted.close()


def test_explicit_reject_does_not_retry(broker):
    broker.inject_submit_fault('reject')
    order = broker.submit(intent())
    assert order.status == 'REJECTED'
    assert broker.submit(intent()) == order
    with pytest.raises(BrokerSafetyError):
        apply(broker, order)


def test_partial_duplicate_out_of_order_execution_and_status(broker):
    order = broker.submit(intent())
    later = apply(broker, order, 3, 'late', seconds=3)
    assert apply(broker, order, 3, 'late', seconds=3) is None
    earlier = apply(broker, order, 2, 'early', seconds=1)
    assert earlier.at < later.at
    assert broker.orders()[0].filled_quantity == 5
    broker.on_order_status(order.order_id, 'PARTIALLY_FILLED', 5, sequence=8)
    stale = broker.on_order_status(order.order_id, 'SUBMITTED', 0, sequence=9)
    assert stale.status == 'PARTIALLY_FILLED'
    assert broker.on_order_status(order.order_id, 'PENDING', 5, sequence=7) == stale
    with pytest.raises(BrokerSafetyError, match='Conflicting duplicate'):
        apply(broker, order, 3, 'late', seconds=3, price=D('101'))
    with pytest.raises(ValueError, match='overfills'):
        apply(broker, order, 6, 'too-much')
    apply(broker, order, 5, 'last', seconds=4)
    assert broker.orders()[0].status == 'FILLED'
    assert len(broker.fills()) == 3
    assert broker.on_order_status(order.order_id, 'SUBMITTED', 10).status == 'FILLED'


def test_status_ahead_of_execution_never_fabricates_fills(broker):
    order = broker.submit(intent())
    reported = broker.on_order_status(order.order_id, 'FILLED', 10)
    assert reported.status == 'UNKNOWN'
    assert reported.filled_quantity == 0
    assert broker.fills() == []
    assert not broker.status()['can_submit']
    assert not broker.reconcile(account())['reconciled']
    apply(broker, order, 4, 'first')
    assert not broker.reconcile(account({'SPY': 4}))['reconciled']
    apply(broker, order, 6, 'second')
    assert broker.reconcile(account({'SPY': 10}))['reconciled']


@pytest.mark.parametrize('authorization', [False, None, 1, 'true'])
def test_cancel_requires_literal_authorization_even_terminal(broker, authorization):
    order = broker.submit(intent())
    with pytest.raises(BrokerSafetyError, match='authorization'):
        broker.cancel(order.order_id, authorization)
    broker.cancel(order.order_id, authorized=True)
    with pytest.raises(BrokerSafetyError, match='authorization'):
        broker.cancel(order.order_id, authorization)
    assert broker.fills() == []


def test_pending_cancel_partial_fill_does_not_become_submitted(broker):
    order = broker.submit(intent())
    broker.inject_cancel_timeout()
    assert broker.cancel(order.order_id, authorized=True).status == 'PENDING_CANCEL'
    apply(broker, order, 3)
    assert broker.on_order_status(order.order_id, 'SUBMITTED', 3).status == 'PENDING_CANCEL'
    assert broker.cancel(order.order_id, authorized=True).status == 'PENDING_CANCEL'
    assert not broker.reconcile(account({'SPY': 3}))['reconciled']
    broker.on_order_status(order.order_id, 'CANCELLED', 3, at=AT + timedelta(seconds=5))
    assert broker.reconcile(account({'SPY': 3}))['reconciled']
    assert broker.orders()[0].filled_quantity == 3


def test_cancel_callback_duplicates_cannot_extend_execution_window(broker):
    order = broker.submit(intent())
    broker.on_order_status(order.order_id, 'CANCELLED', 0, at=AT + timedelta(seconds=5))
    broker.on_order_status(order.order_id, 'CANCELLED', 0, at=AT + timedelta(seconds=9))
    apply(broker, order, 3, seconds=4)
    with pytest.raises(BrokerSafetyError, match='after confirmed'):
        apply(broker, order, 1, 'invalid', seconds=6)
    assert broker.orders()[0].status == 'CANCELLED'
    assert broker.orders()[0].filled_quantity == 3


def test_disconnect_and_restart_require_explicit_reconciliation(tmp_path):
    path = tmp_path / 'restart.sqlite'
    original = SimulatedBroker(path)
    order = original.submit(intent())
    original.disconnect()
    with pytest.raises(BrokerSafetyError):
        original.cancel(order.order_id, authorized=True)
    apply(original, order, 2)  # Late evidence is allowed, but not new sends.
    original.reconnect()
    with pytest.raises(BrokerSafetyError):
        original.submit(intent('new'))
    original.close()
    restarted = SimulatedBroker(path)
    try:
        assert not restarted.status()['reconciled']
        assert len(restarted.fills()) == 1
        assert restarted.reconcile(account({'SPY': 2}))['reconciled']
        assert restarted.submit(intent()).filled_quantity == 2
    finally:
        restarted.close()


@pytest.mark.parametrize('snapshot', [account(), account({'SPY': 9}),
                                      account({'SPY': 10, 'QQQ': 1}), account({'QQQ': 10})])
def test_reconcile_must_match_local_fills_not_just_nonnegative(broker, snapshot):
    order = broker.submit(intent())
    apply(broker, order)
    result = broker.reconcile(snapshot)
    assert result['reconciled'] is False
    assert 'POSITION_MISMATCH' in result['issues']
    assert result['expected_positions'] == {'SPY': 10}
    assert result['position_mismatches']
    with pytest.raises(BrokerSafetyError):
        broker.submit(intent('blocked'))
    assert broker.reconcile(account({'SPY': 10, 'ZERO': 0}))['reconciled']


def test_buy_sell_net_position_reconcile(broker):
    apply(broker, broker.submit(intent()), 10, 'buy')
    apply(broker, broker.submit(intent('sell', side='SELL', quantity=6)), 6, 'sell')
    assert broker.reconcile(account({'SPY': 4}))['reconciled']
    assert not broker.reconcile(account({'SPY': 10}))['reconciled']


def test_no_silent_adoption_of_external_holdings(broker):
    assert not broker.reconcile(account({'SPY': 20}))['reconciled']
    assert broker.reconcile()['expected_positions'] == {}


def test_explicit_baseline_persists_and_cannot_be_rebased(tmp_path):
    path = tmp_path / 'baseline.sqlite'
    original = SimulatedBroker(path, baseline_positions={'SPY': 20})
    apply(original, original.submit(intent(side='SELL')), 10)
    assert original.reconcile(account({'SPY': 10}))['reconciled']
    original.close()
    with pytest.raises(BrokerSafetyError, match='baseline cannot be changed'):
        SimulatedBroker(path, baseline_positions={'SPY': 10})
    restarted = SimulatedBroker(path)
    try:
        assert restarted.reconcile(account({'SPY': 10}))['reconciled']
        assert restarted.reconcile()['expected_positions'] == {'SPY': 10}
    finally:
        restarted.close()


@pytest.mark.parametrize('baseline', [{'SPY': -1}, {'SPY': True}, {'SPY': D('2')}, {'': 2}, []])
def test_invalid_baseline_is_refused(tmp_path, baseline):
    with pytest.raises(ValueError):
        SimulatedBroker(tmp_path / 'bad.sqlite', baseline_positions=baseline)


def test_negative_derived_holdings_fail_even_without_account(broker):
    apply(broker, broker.submit(intent(side='SELL')))
    assert not broker.status()['can_submit']
    assert 'NEGATIVE_DERIVED_POSITION' in broker.reconcile()['issues']


@pytest.mark.parametrize('snapshot,issue', [
    (account(cash=D('NaN')), 'INVALID_OR_UNRECONCILED_ACCOUNT'),
    (account(equity=D('-1')), 'INVALID_OR_UNRECONCILED_ACCOUNT'),
    (account(reconciled=False), 'INVALID_OR_UNRECONCILED_ACCOUNT'),
    (account(cash=100), 'INVALID_OR_UNRECONCILED_ACCOUNT'),
    (account({'SPY': -1}), 'INVALID_POSITION'),
    (account({'SPY': True}), 'INVALID_POSITION'),
    (account(as_of=AT.replace(tzinfo=None)), 'INVALID_ACCOUNT_TIMESTAMP'),
    (account(positions={'SPY': Position('QQQ', 1, D('1'), D('1'))}), 'INVALID_POSITION'),
    (account(positions={'SPY': Position('SPY', 1, D('NaN'), D('1'))}), 'INVALID_POSITION'),
    ('not an account', 'INVALID_ACCOUNT'),
])
def test_invalid_account_fails_closed(broker, snapshot, issue):
    result = broker.reconcile(snapshot)
    assert not result['reconciled']
    assert issue in result['issues']
    assert not broker.status()['can_submit']


def test_snapshot_cannot_precede_local_fills(broker):
    apply(broker, broker.submit(intent()))
    result = broker.reconcile(account({'SPY': 10}, as_of=AT))
    assert 'ACCOUNT_PRECEDES_LOCAL_FILLS' in result['issues']
    assert not result['reconciled']


@pytest.mark.parametrize('changes', [dict(quantity=True), dict(quantity=0), dict(quantity=1.5),
                                     dict(price=D('NaN')), dict(price=100.0), dict(price=D('0.00001')),
                                     dict(commission=D('-1')), dict(at=AT.replace(tzinfo=None)),
                                     dict(at=AT - timedelta(seconds=1))])
def test_invalid_fill_does_not_mutate_journal(broker, changes):
    order = broker.submit(intent())
    with pytest.raises((ValueError, BrokerSafetyError)):
        apply(broker, order, **changes)
    assert broker.fills() == []
    assert broker.orders()[0].filled_quantity == 0


def test_failed_account_check_cannot_be_bypassed_by_omission_or_restart(tmp_path):
    path = tmp_path / 'failed-account.sqlite'
    original = SimulatedBroker(path)
    apply(original, original.submit(intent()))
    assert not original.reconcile(account())['reconciled']
    assert original.status()['account_check_required']
    assert 'ACCOUNT_RECHECK_REQUIRED' in original.reconcile()['issues']
    original.close()
    restarted = SimulatedBroker(path)
    try:
        assert not restarted.status()['can_submit']
        assert not restarted.reconcile()['reconciled']
        assert restarted.reconcile(account({'SPY': 10}))['reconciled']
        assert not restarted.status()['account_check_required']
    finally:
        restarted.close()


def test_unknown_status_callback_blocks_new_orders(broker):
    order = broker.submit(intent())
    broker.on_order_status(order.order_id, 'UNKNOWN', 0)
    assert not broker.status()['can_submit']
    with pytest.raises(BrokerSafetyError):
        broker.submit(intent('blocked'))


def test_money_rounding_and_concurrent_idempotency(broker):
    with ThreadPoolExecutor(max_workers=4) as pool:
        orders = list(pool.map(lambda _: broker.submit(intent()), range(12)))
    assert len({o.order_id for o in orders}) == 1
    order = orders[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        fills = list(pool.map(lambda _: apply(broker, order, price=D('100.12345'),
                                            commission=D('1.00005')), range(12)))
    assert sum(f is not None for f in fills) == 1
    assert broker.fills()[0].price == D('100.1234')
    assert broker.fills()[0].commission == D('1.0000')
