from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

import quantlab.risk as risk_module
from quantlab.models import AccountSnapshot, OrderIntent, Position
from quantlab.risk import RiskDecision, RiskEngine, RiskLimits

D = Decimal
UTC = timezone.utc
NOW = datetime(2024, 3, 11, 14, 0, tzinfo=UTC)


def account(cash='100000', holdings=None, at=NOW, day_start=None, peak=None):
    positions = {symbol: Position(symbol, quantity, D('100'), D('100'))
                 for symbol, quantity in (holdings or {}).items()}
    equity = D(cash) + sum((p.quantity * p.market_price for p in positions.values()), D('0'))
    return AccountSnapshot(D(cash), equity, positions, at, True,
                           D(day_start) if day_start is not None else equity,
                           D(peak) if peak is not None else equity)


def intent(key='test', *, side='BUY', quantity=10, price='100', at=NOW, symbol='SPY'):
    return OrderIntent(key, 'client-' + key, symbol, side, quantity, at, D(price))


def check(engine, order=None, snapshot=None, *, at=NOW, price=D('100'), quote_at=None, **kwargs):
    return engine.check(order or intent(at=at), snapshot or account(at=at),
                        price, quote_at if quote_at is not None else at, at, **kwargs)


@pytest.fixture
def engine():
    value = RiskEngine(':memory:')
    yield value
    value.close()


def permissive(**kwargs):
    values = dict(max_position_weight=D('1'), max_position_notional=D('1000000'),
                  max_order_notional=D('1000000'), max_gross_exposure=D('1'),
                  min_cash_buffer=D('0'))
    values.update(kwargs)
    return RiskLimits(**values)


def assert_refused(decision, reason):
    assert decision.allowed is False
    assert reason in decision.reasons, decision.reasons


def test_contract_defaults_and_serializable_status(engine):
    limits = RiskLimits()
    assert is_dataclass(limits)
    assert is_dataclass(RiskDecision)
    assert limits.max_position_weight == D('0.4')
    assert limits.max_order_notional == D('50000')
    assert limits.max_gross_exposure == D('0.9')
    assert limits.min_cash_buffer == D('0.05')
    assert limits.max_leverage == D('1')
    assert RiskDecision(True).reasons == []
    assert engine.status()['halted'] is False
    assert engine.status()['limits']['max_order_notional'] == '50000'
    assert engine.status()['storage_available'] is True


@pytest.mark.parametrize('field,value', [
    ('max_position_weight', D('1.1')), ('max_position_weight', 0.4),
    ('max_position_weight', D('NaN')), ('max_position_notional', D('Infinity')),
    ('max_order_notional', D('0')), ('max_gross_exposure', D('-1')),
    ('max_leverage', D('1.01')), ('min_cash_buffer', D('1')),
    ('min_cash_buffer', D('-0.1')), ('max_daily_loss', D('0')),
    ('max_drawdown', D('Infinity')), ('max_open_orders', True),
    ('max_orders_per_minute', 0), ('max_orders_per_minute', 1.5),
    ('max_quote_age_seconds', -1), ('max_account_age_seconds', 0),
    ('max_price_deviation', D('1')), ('fee_per_share', D('-1')),
    ('minimum_fee', 1.0), ('execution_buffer', D('NaN')),
])
def test_invalid_limits(field, value):
    with pytest.raises(ValueError):
        RiskLimits(**{field: value})


def test_zero_fee_budget_is_not_silently_accepted():
    with pytest.raises(ValueError, match='fee budget'):
        RiskLimits(minimum_fee=D('0'), fee_per_share=D('0'))


def test_check_records_but_never_changes_account_or_order(engine):
    snapshot, order = account(), intent()
    original = deepcopy((snapshot, order))
    result = check(engine, order, snapshot)
    assert result == RiskDecision(True, [])
    assert (snapshot, order) == original
    checks = [entry for entry in engine.audit() if entry['event'] == 'check']
    assert len(checks) == 1
    audit = checks[0]
    assert audit['allowed'] is True
    assert audit['details']['fee_budget'] == '1.0000'
    assert audit['details']['order_notional'] == '1001.0000'
    assert audit['details']['projected_cash_after_reserves'] == '98998.0000'
    assert engine.status()['orders_in_window'] == 1


def test_whole_position_sell_is_allowed_without_mutation(engine):
    snapshot = account('90000', {'SPY': 100})
    original = deepcopy(snapshot)
    assert check(engine, intent(side='SELL', quantity=100), snapshot).allowed
    assert snapshot == original


@pytest.mark.parametrize('holdings,quantity', [({}, 1), ({'SPY': 5}, 6)])
def test_no_shorting(engine, holdings, quantity):
    assert_refused(check(engine, intent(side='SELL', quantity=quantity),
                         account(holdings=holdings)), 'insufficient_shares')


def test_position_weight_independent_of_strategy(engine):
    assert_refused(check(engine, intent(quantity=60), account('65000', {'SPY': 350})),
                   'position_weight_limit')


def test_position_notional_limit():
    engine = RiskEngine(':memory:', permissive(max_position_notional=D('1000')))
    try:
        assert_refused(check(engine, intent(quantity=11)), 'position_notional_limit')
    finally:
        engine.close()


def test_order_notional_limit():
    engine = RiskEngine(':memory:', permissive(max_order_notional=D('50000')))
    try:
        assert_refused(check(engine, intent(quantity=500)), 'order_notional_limit')
        # Conservative price reserve is deliberately part of the order budget.
        assert check(engine, intent('smaller', quantity=499)).allowed
    finally:
        engine.close()


def test_gross_exposure_includes_other_symbols(engine):
    snapshot = account('10000', {'AAA': 300, 'BBB': 300, 'CCC': 300})
    assert_refused(check(engine, intent(quantity=1), snapshot), 'gross_exposure_limit')


def test_cash_buffer_even_without_position_and_exposure_caps():
    engine = RiskEngine(':memory:', permissive(min_cash_buffer=D('0.05')))
    try:
        assert_refused(check(engine, intent(quantity=95), account('10000')), 'cash_buffer_limit')
        assert check(engine, intent('less', quantity=94), account('10000')).allowed
    finally:
        engine.close()


def test_fee_budget_prevents_borrowing():
    engine = RiskEngine(':memory:', permissive(execution_buffer=D('0')))
    try:
        result = check(engine, intent(quantity=1), account('100.50'))
        assert_refused(result, 'insufficient_cash')
        assert 'leverage_limit' in result.reasons
        assert next(entry for entry in engine.audit() if entry['event'] == 'check')['details']['fee_budget'] == '1.0000'
    finally:
        engine.close()


def test_per_share_fee_budget():
    engine = RiskEngine(':memory:', permissive(execution_buffer=D('0')))
    try:
        assert_refused(check(engine, intent(quantity=1000, price='1'), account('1001'),
                             price=D('1')), 'insufficient_cash')
        assert next(entry for entry in engine.audit() if entry['event'] == 'check')['details']['fee_budget'] == '5.0000'
    finally:
        engine.close()


def test_low_cash_does_not_bypass_quote_staleness(engine):
    result = check(engine, intent(quantity=1), account('10'),
                   quote_at=NOW - timedelta(minutes=2))
    assert_refused(result, 'stale_quote')
    assert 'insufficient_cash' in result.reasons


def test_reservations_reduce_cash_and_count_as_exposure(engine):
    snapshot = account('10000', {'AAA': 300, 'BBB': 300, 'CCC': 300})
    result = check(engine, intent(quantity=11), snapshot, open_orders=2, reserved_cash=D('9000'))
    assert_refused(result, 'insufficient_cash')
    assert 'gross_exposure_limit' in result.reasons


def test_reservations_conservatively_count_towards_symbol(engine):
    result = check(engine, intent(quantity=10), account('70000', {'QQQ': 300}),
                   open_orders=2, reserved_cash=D('40000'))
    assert_refused(result, 'position_weight_limit')


def test_sell_proceeds_cannot_hide_over_reserved_cash(engine):
    assert_refused(check(engine, intent(side='SELL', quantity=50),
                         account('1000', {'SPY': 100}), reserved_cash=D('2000')),
                   'reserved_cash_exceeds_cash')


@pytest.mark.parametrize('kwargs,reason', [
    ({'open_orders': 20}, 'pending_order_limit'),
    ({'open_orders': -1}, 'invalid_open_orders'),
    ({'open_orders': True}, 'invalid_open_orders'),
    ({'open_orders': 1.2}, 'invalid_open_orders'),
    ({'reserved_cash': D('-1')}, 'invalid_reserved_cash'),
    ({'reserved_cash': D('NaN')}, 'invalid_reserved_cash'),
    ({'reserved_cash': 1.0}, 'invalid_reserved_cash'),
    ({'open_orders': 1}, 'pending_reservations_unknown'),
    ({'market_open': False}, 'market_closed'),
    ({'market_open': None}, 'market_state_unknown'),
    ({'market_open': 1}, 'market_state_unknown'),
])
def test_invalid_or_closed_execution_context(engine, kwargs, reason):
    assert_refused(check(engine, **kwargs), reason)


def test_pending_sell_quantities_cannot_be_assumed_available(engine):
    assert_refused(check(engine, intent(side='SELL'), account('90000', {'SPY': 100}),
                         open_orders=1, reserved_cash=D('100')), 'pending_sell_quantity_unknown')


def test_accurate_pending_buy_budget_is_accepted(engine):
    assert check(engine, open_orders=2, reserved_cash=D('1000')).allowed


@pytest.mark.parametrize('quote,reason', [
    (D('0'), 'invalid_quote_price'), (D('-1'), 'invalid_quote_price'),
    (D('NaN'), 'invalid_quote_price'), (D('Infinity'), 'invalid_quote_price'),
    (100.0, 'invalid_quote_price'), (None, 'invalid_quote_price'),
    (D('104'), 'price_deviation'), (D('96'), 'price_deviation'),
])
def test_external_quote_validation(engine, quote, reason):
    assert_refused(check(engine, price=quote), reason)


def test_price_deviation_boundary(engine):
    assert check(engine, price=D('103')).allowed


@pytest.mark.parametrize('delta,allowed,reason', [
    (timedelta(seconds=60), True, None),
    (timedelta(seconds=60, microseconds=1), False, 'stale_quote'),
    (timedelta(microseconds=-1), False, 'future_quote'),
])
def test_quote_age_boundaries(engine, delta, allowed, reason):
    result = check(engine, quote_at=NOW - delta)
    assert result.allowed is allowed
    if reason:
        assert reason in result.reasons


@pytest.mark.parametrize('field', ['now', 'quote', 'account', 'intent'])
@pytest.mark.parametrize('tz', [None, timezone(timedelta(hours=1))])
def test_all_timestamps_must_be_utc(engine, field, tz):
    bad = NOW.replace(tzinfo=tz)
    snapshot, order = account(), intent()
    now, quote_at = NOW, NOW
    if field == 'now':
        now = bad
    elif field == 'quote':
        quote_at = bad
    elif field == 'account':
        snapshot.as_of = bad
    else:
        object.__setattr__(order, 'created_at', bad)
    result = engine.check(order, snapshot, D('100'), quote_at, now)
    assert_refused(result, {'now': 'now_not_utc', 'quote': 'quote_not_utc',
                            'account': 'account_not_utc', 'intent': 'invalid_intent'}[field])


@pytest.mark.parametrize('field,reason', [('intent', 'future_intent'), ('account', 'future_account')])
def test_future_non_quote_timestamps(engine, field, reason):
    snapshot, order = account(), intent()
    if field == 'intent':
        order = intent(at=NOW + timedelta(seconds=1))
    else:
        snapshot.as_of += timedelta(seconds=1)
    assert_refused(check(engine, order, snapshot), reason)


def test_stale_snapshot_not_observed(engine):
    snapshot = account(at=NOW - timedelta(seconds=301))
    assert_refused(check(engine, snapshot=snapshot), 'stale_account')
    assert engine.status()['peak_equity'] is None


@pytest.mark.parametrize('value', [False, None, 1, 'yes'])
def test_only_explicitly_reconciled_account_is_usable(engine, value):
    snapshot = account()
    snapshot.reconciled = value
    assert_refused(check(engine, snapshot=snapshot), 'account_unreconciled')
    assert engine.status()['day'] is None


@pytest.mark.parametrize('field,value', [
    ('cash', D('-1')), ('cash', D('NaN')), ('cash', 100000),
    ('equity', D('0')), ('equity', D('Infinity')),
    ('day_start_equity', D('0')), ('peak_equity', D('-1')),
])
def test_invalid_account_numbers(engine, field, value):
    snapshot = account()
    setattr(snapshot, field, value)
    assert_refused(check(engine, snapshot=snapshot), 'invalid_account')


def test_inconsistent_equity_cannot_inflate_limits(engine):
    snapshot = account()
    snapshot.equity = D('200000')
    assert_refused(check(engine, snapshot=snapshot), 'account_equity_mismatch')


@pytest.mark.parametrize('field,value', [
    ('quantity', -1), ('quantity', True), ('quantity', 1.5),
    ('market_price', D('0')), ('market_price', D('NaN')),
    ('average_cost', D('-1')), ('symbol', 'WRONG'),
])
def test_invalid_position(engine, field, value):
    snapshot = account('90000', {'SPY': 100})
    setattr(snapshot.positions['SPY'], field, value)
    assert_refused(check(engine, snapshot=snapshot), 'invalid_account')


@pytest.mark.parametrize('mode,reason', [
    ('LIVE', 'unsupported_environment'), ('UNKNOWN', 'unsupported_environment'),
    (None, 'unsupported_environment'), ('IBKR_PAPER', 'environment_unverified'),
])
def test_environment_not_inferred_from_account_prefix(engine, mode, reason):
    order = intent()
    object.__setattr__(order, 'mode', mode)
    assert_refused(check(engine, order), reason)


@pytest.mark.parametrize('field,value', [
    ('quantity', 0), ('quantity', True), ('quantity', 1.5), ('side', 'SHORT'),
    ('symbol', ''), ('intent_id', ''), ('client_order_id', None),
    ('reference_price', D('NaN')), ('tif', 'IOC'),
])
def test_malformed_intents_fail_closed(engine, field, value):
    order = intent()
    object.__setattr__(order, field, value)
    assert_refused(check(engine, order), 'invalid_intent')


@pytest.mark.parametrize('at,allowed', [
    (datetime(2024, 3, 8, 14, 29, 59, tzinfo=UTC), False),
    (datetime(2024, 3, 8, 14, 30, tzinfo=UTC), True),
    (datetime(2024, 3, 11, 13, 29, 59, tzinfo=UTC), False),
    (datetime(2024, 3, 11, 13, 30, tzinfo=UTC), True),
    (datetime(2024, 3, 11, 20, 0, tzinfo=UTC), False),
    (datetime(2024, 3, 9, 16, 0, tzinfo=UTC), False),
    (datetime(2024, 11, 28, 16, 0, tzinfo=UTC), False),
    (datetime(2024, 11, 29, 17, 59, 59, tzinfo=UTC), True),
    (datetime(2024, 11, 29, 18, 0, tzinfo=UTC), False),
    (datetime(2001, 9, 11, 15, 0, tzinfo=UTC), False),
])
def test_actual_xnys_calendar_dst_holiday_early_close(engine, at, allowed):
    result = check(engine, at=at)
    assert result.allowed is allowed, result.reasons
    if not allowed:
        assert 'outside_market_session' in result.reasons


def test_calendar_failure_is_not_weekday_fallback(engine, monkeypatch):
    def unavailable(now):
        raise ImportError('calendar missing')
    monkeypatch.setattr(risk_module, '_session_times', unavailable)
    assert_refused(check(engine), 'market_calendar_unavailable')


def test_persistent_manual_halt_does_not_cancel_or_liquidate(tmp_path):
    path = tmp_path / 'shared.sqlite'
    snapshot = account('90000', {'SPY': 100})
    original = deepcopy(snapshot)
    engine = RiskEngine(path)
    engine.set_halt(True, 'operator request')
    assert_refused(check(engine, snapshot=snapshot), 'halted')
    assert_refused(check(engine, intent('sell', side='SELL'), snapshot), 'halted')
    engine.close()
    engine = RiskEngine(path)
    try:
        assert engine.status()['halted'] is True
        assert engine.status()['halt_reason'] == 'operator request'
        assert snapshot == original
        halt_event = next(entry for entry in engine.audit() if entry['event'] == 'halt')
        assert halt_event['event'] == 'halt'
        assert halt_event['details']['cancels_orders'] is False
        assert halt_event['details']['liquidates_positions'] is False
        assert_refused(check(engine, snapshot=snapshot), 'halted')
        engine.set_halt(False, 'review completed')
        assert check(engine, snapshot=snapshot).allowed
    finally:
        engine.close()


@pytest.mark.parametrize('halted,reason', [(1, 'bad'), (True, ''), (False, None)])
def test_halt_requires_explicit_bool_and_reason(engine, halted, reason):
    with pytest.raises(ValueError):
        engine.set_halt(halted, reason)
    assert engine.status()['halted'] is False


def test_daily_loss_latches_through_restart_and_recovery_until_ny_rollover(tmp_path):
    path = tmp_path / 'risk.sqlite'
    engine = RiskEngine(path)
    assert check(engine, intent('initial')).allowed
    later = NOW + timedelta(minutes=1)
    assert_refused(check(engine, intent('loss', at=later),
                         account('97000', at=later, day_start='100000', peak='100000'), at=later),
                   'daily_loss_limit')
    engine.close()
    engine = RiskEngine(path)
    try:
        assert engine.status()['daily_loss'] == '0.03'
        # A lower baseline and even a recovery must not erase the observed breach.
        engine.set_halt(False, 'manual halt cleared, not loss state')
        assert_refused(check(engine, intent('recovery', at=later),
                             account('100000', at=later, day_start='90000', peak='90000'), at=later),
                       'daily_loss_limit')
        tomorrow = NOW + timedelta(days=1)
        assert check(engine, intent('next-day', at=tomorrow),
                     account('97000', at=tomorrow, day_start='97000', peak='97000'), at=tomorrow).allowed
        status = engine.status()
        assert status['day'] == '2024-03-12'
        assert status['day_start_equity'] == '97000.0000'
        assert status['peak_equity'] == '100000.0000'
        assert status['daily_loss_breached'] is False
    finally:
        engine.close()


def test_ny_date_not_utc_midnight_controls_daily_baseline(engine):
    assert check(engine, intent('initial')).allowed
    overnight = datetime(2024, 3, 12, 0, 30, tzinfo=UTC)  # Still March 11 in New York.
    assert_refused(check(engine, intent('night', at=overnight),
                         account('97000', at=overnight, day_start='97000'), at=overnight),
                   'daily_loss_limit')
    assert engine.status()['day'] == '2024-03-11'
    assert engine.status()['day_start_equity'] == '100000.0000'


def test_high_water_drawdown_survives_restart_and_new_day(tmp_path):
    path = tmp_path / 'risk.sqlite'
    engine = RiskEngine(path)
    assert check(engine, intent('peak'), account('120000')).allowed
    engine.close()
    engine = RiskEngine(path)
    try:
        tomorrow = NOW + timedelta(days=1)
        result = check(engine, intent('drawdown', at=tomorrow),
                       account('96000', at=tomorrow), at=tomorrow)
        assert_refused(result, 'drawdown_limit')
        assert 'daily_loss_limit' not in result.reasons
        assert engine.status()['peak_equity'] == '120000.0000'
        assert D(engine.status()['drawdown']) == D('0.20')
    finally:
        engine.close()


def test_snapshot_previous_high_water_is_honored(engine):
    assert_refused(check(engine, snapshot=account('70000', peak='100000')), 'drawdown_limit')


def test_time_regression_cannot_roll_back_loss_or_rate(engine):
    assert check(engine).allowed
    earlier = NOW - timedelta(seconds=1)
    assert_refused(check(engine, intent('earlier', at=earlier), account('1000000', at=earlier),
                         at=earlier), 'time_regression')
    assert engine.status()['peak_equity'] == '100000.0000'
    assert engine.status()['last_checked_at'] == NOW.isoformat()
    assert engine.status()['orders_in_window'] == 1


def test_account_timestamp_regression_does_not_replace_loss_state(engine):
    assert check(engine).allowed
    later = NOW + timedelta(seconds=1)
    assert_refused(check(engine, intent('later', at=later), account(at=NOW - timedelta(seconds=1)),
                         at=later), 'account_time_regression')
    assert engine.status()['last_account_at'] == NOW.isoformat()


def test_rate_persists_and_expires_at_exact_window_boundary(tmp_path):
    path = tmp_path / 'risk.sqlite'
    limits = RiskLimits(max_orders_per_minute=2)
    engine = RiskEngine(path, limits)
    assert check(engine, intent('a')).allowed
    assert check(engine, intent('b')).allowed
    denied = intent('c')
    assert_refused(check(engine, denied), 'order_rate_limit')
    engine.close()
    engine = RiskEngine(path, limits)
    try:
        assert engine.status()['orders_in_window'] == 2
        later = NOW + timedelta(seconds=59)
        assert_refused(check(engine, denied, account(at=later), at=later), 'order_rate_limit')
        later = NOW + timedelta(seconds=60)
        assert check(engine, denied, account(at=later), at=later).allowed
        assert engine.status()['orders_in_window'] == 1
    finally:
        engine.close()


def test_approved_duplicate_rejected_and_rechecked_even_after_restart(tmp_path):
    path = tmp_path / 'risk.sqlite'
    engine = RiskEngine(path)
    order = intent()
    assert check(engine, order).allowed
    engine.close()
    engine = RiskEngine(path)
    try:
        engine.set_halt(True, 'stop')
        result = check(engine, order, quote_at=NOW - timedelta(minutes=5))
        assert_refused(result, 'duplicate_intent')
        assert 'halted' in result.reasons
        assert 'stale_quote' in result.reasons
        assert engine.status()['orders_in_window'] == 1
        assert len([a for a in engine.audit() if a['event'] == 'check']) == 2
    finally:
        engine.close()


@pytest.mark.parametrize('order', [intent(quantity=20), replace(intent('new'), client_order_id='client-test'),
                                   replace(intent(), client_order_id='other-client')])
def test_id_or_client_id_reuse_with_different_order_is_conflict(engine, order):
    assert check(engine).allowed
    assert_refused(check(engine, order), 'intent_id_conflict')


def test_denied_intent_retry_requires_unchanged_payload(engine):
    order = intent()
    assert_refused(check(engine, order, market_open=False), 'market_closed')
    assert engine.status()['orders_in_window'] == 0
    assert_refused(check(engine, intent(quantity=20)), 'intent_id_conflict')
    assert check(engine, order).allowed


def test_memory_database_is_retained_but_independent(engine):
    engine.set_halt(True, 'persist within instance')
    other = RiskEngine(':memory:')
    try:
        assert engine.status()['halted'] is True
        assert other.status()['halted'] is False
        assert check(other).allowed
    finally:
        other.close()


def test_shared_database_preserves_other_tables(tmp_path):
    path = tmp_path / 'shared.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE broker_orders (id TEXT PRIMARY KEY, status TEXT)')
        db.execute("INSERT INTO broker_orders VALUES ('pending', 'SUBMITTED')")
    engine = RiskEngine(path)
    assert check(engine).allowed
    engine.set_halt(True, 'do not touch broker state')
    engine.close()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM broker_orders').fetchall() == [('pending', 'SUBMITTED')]


@pytest.mark.parametrize('shared_connection', [True, False])
def test_concurrent_checks_cannot_exceed_rate_limit(tmp_path, shared_connection):
    limits = RiskLimits(max_orders_per_minute=2)
    first = RiskEngine(':memory:' if shared_connection else tmp_path / 'risk.sqlite', limits)
    engines = [first] * 8 if shared_connection else [first] + [
        RiskEngine(tmp_path / 'risk.sqlite', limits) for _ in range(7)]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: check(engines[i], intent(str(i))), range(8)))
        assert sum(result.allowed for result in results) == 2
        assert first.status()['orders_in_window'] == 2
        assert len([entry for entry in first.audit() if entry['event'] == 'check']) == 8
    finally:
        for engine in set(engines):
            engine.close()


def test_concurrent_identical_intent_is_authorized_at_most_once(engine):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: check(engine), range(6)))
    assert sum(result.allowed for result in results) == 1
    assert engine.status()['orders_in_window'] == 1


def test_storage_failure_never_authorizes_or_claims_durable_audit():
    engine = RiskEngine(':memory:')
    engine.close()
    assert_refused(check(engine), 'risk_state_or_input_unavailable')
    assert engine.status()['storage_available'] is False
    assert engine.status()['halted'] is True


def test_failed_audit_transaction_rolls_back_approval(engine):
    engine._db.execute('''CREATE TRIGGER quantlab_risk_test_audit_failure
        BEFORE INSERT ON quantlab_risk_audit BEGIN SELECT RAISE(ABORT, 'unwritable'); END''')
    assert_refused(check(engine), 'risk_state_or_input_unavailable')
    assert engine.status()['orders_in_window'] == 0
    assert engine.status()['peak_equity'] is None
    assert engine._db.execute('SELECT COUNT(*) FROM quantlab_risk_intents').fetchone()[0] == 0
    engine._db.execute('DROP TRIGGER quantlab_risk_test_audit_failure')
    assert check(engine).allowed


def test_corrupt_state_is_not_reset_to_permissive_defaults(engine):
    engine._db.execute("UPDATE quantlab_risk_state SET payload = '{}' WHERE singleton = 1")
    assert_refused(check(engine), 'risk_state_or_input_unavailable')
    assert engine.status()['storage_available'] is False
    assert engine.status()['halted'] is True


def test_money_and_audit_are_four_decimal_half_even(engine):
    engine = RiskEngine(':memory:', RiskLimits(fee_per_share=D('0.100005'), minimum_fee=D('0.1')))
    try:
        assert check(engine).allowed
        assert next(entry for entry in engine.audit() if entry['event'] == 'check')['details']['fee_budget'] == '1.0000'
        assert all(isinstance(a['at'], str) and a['at'].endswith('+00:00') for a in engine.audit())
    finally:
        engine.close()
