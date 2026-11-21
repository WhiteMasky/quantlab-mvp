from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import subprocess
import sys

import pytest

from quantlab.models import AccountSnapshot, OrderIntent, Position, TargetWeight, money
from quantlab.portfolio import rebalance_orders

D = Decimal
ZERO = D('0')
AS_OF = datetime(2024, 1, 5, 22, tzinfo=timezone.utc)
KEY = '2024-01-05:trend:fixture:simulated-account'


def account(cash='1000', holdings=None):
    positions = {
        symbol: Position(symbol, quantity, D(price), D(price))
        for symbol, (quantity, price) in (holdings or {}).items()
    }
    equity = money(D(cash) + sum((item.quantity * item.market_price for item in positions.values()), ZERO))
    return AccountSnapshot(money(cash), equity, positions, AS_OF)


def target(symbol, weight):
    return TargetWeight(symbol, D(weight), 'unit-test target')


def legs(orders):
    assert all(isinstance(order, OrderIntent) for order in orders)
    assert all(type(order.quantity) is int and order.quantity > 0 for order in orders)
    assert all(isinstance(order.reference_price, Decimal) for order in orders)
    assert all(order.tif == 'DAY' and order.mode == 'SIMULATED' for order in orders)
    assert [order.side for order in orders] == sorted((order.side for order in orders), reverse=True)
    return [(order.symbol, order.side, order.quantity) for order in orders]


def settle(snapshot, orders, *, bps=D('6'), per_share=D('0.005'), minimum=D('1')):
    """Independent whole-share/cash check using shared monetary conventions."""
    cash = snapshot.cash
    quantities = {symbol: item.quantity for symbol, item in snapshot.positions.items()}
    for order in orders:
        direction = D('1') if order.side == 'BUY' else D('-1')
        execution_price = money(order.reference_price * (D('1') + direction * bps / D('10000')))
        notional = money(order.quantity * execution_price)
        commission = money(max(minimum, per_share * order.quantity))
        if order.side == 'SELL':
            assert order.quantity <= quantities.get(order.symbol, 0)
            quantities[order.symbol] -= order.quantity
            cash = money(cash + notional - commission)
        else:
            quantities[order.symbol] = quantities.get(order.symbol, 0) + order.quantity
            cash = money(cash - notional - commission)
        assert cash >= ZERO
        assert all(quantity >= 0 for quantity in quantities.values())
    return cash, quantities


def test_whole_share_buys_reserve_default_fees_execution_cost_and_cash():
    snapshot = account()
    orders = rebalance_orders([target('AAA', '1')], snapshot, {'AAA': D('100')}, AS_OF, KEY)
    assert legs(orders) == [('AAA', 'BUY', 8)]
    cash, quantities = settle(snapshot, orders)
    assert cash == D('198.5200')
    assert cash >= money(snapshot.equity * D('0.10'))
    assert quantities == {'AAA': 8}
    # A ninth share fits the raw target but not the cost-inclusive cash budget.
    assert D('9') * D('100.0600') + D('1') > snapshot.cash - money(snapshot.equity * D('0.10'))


@pytest.mark.parametrize(('buffer', 'expected'), [('0', 10), ('0.1', 9), ('0.25', 7)])
def test_cash_buffer_and_whole_share_floor_without_costs(buffer, expected):
    snapshot = account()
    orders = rebalance_orders([target('AAA', '1')], snapshot, {'AAA': D('100')}, AS_OF, KEY,
                              cash_buffer=D(buffer), fee_per_share=ZERO, minimum_fee=ZERO, execution_buffer_bps=ZERO)
    assert legs(orders) == [('AAA', 'BUY', expected)]
    cash, _ = settle(snapshot, orders, bps=ZERO, per_share=ZERO, minimum=ZERO)
    assert cash >= money(snapshot.equity * D(buffer))


def test_full_cash_buffer_makes_no_new_buys():
    assert rebalance_orders([target('AAA', '1')], account(), {'AAA': D('100')}, AS_OF, KEY, cash_buffer=D('1')) == []


def test_multiple_buys_share_cash_and_each_reserves_a_minimum_fee():
    snapshot = account()
    orders = rebalance_orders([target('BBB', '0.5'), target('AAA', '0.5')], snapshot,
                              {'BBB': D('100'), 'AAA': D('100')}, AS_OF, KEY,
                              cash_buffer=ZERO, fee_per_share=ZERO, minimum_fee=D('7'), execution_buffer_bps=ZERO)
    assert legs(orders) == [('AAA', 'BUY', 5), ('BBB', 'BUY', 4)]
    cash, _ = settle(snapshot, orders, bps=ZERO, per_share=ZERO, minimum=D('7'))
    assert cash == D('86')


def test_per_share_fee_above_minimum_is_reserved():
    snapshot = account('10000')
    orders = rebalance_orders([target('AAA', '1')], snapshot, {'AAA': D('10')}, AS_OF, KEY,
                              cash_buffer=ZERO, execution_buffer_bps=ZERO)
    assert legs(orders) == [('AAA', 'BUY', 999)]
    cash, _ = settle(snapshot, orders, bps=ZERO)
    assert cash == D('5.0050')


def test_sells_precede_buys_and_include_removed_holdings():
    snapshot = account('100', {'ZZZ': (10, '50'), 'BBB': (4, '50')})
    targets = [target('AAA', '0.5'), target('BBB', '0.125')]
    orders = rebalance_orders(targets, snapshot, {'AAA': D('100'), 'BBB': D('50'), 'ZZZ': D('50')}, AS_OF, KEY)
    assert legs(orders) == [('BBB', 'SELL', 2), ('ZZZ', 'SELL', 10), ('AAA', 'BUY', 4)]
    cash, quantities = settle(snapshot, orders)
    assert quantities == {'ZZZ': 0, 'BBB': 2, 'AAA': 4}
    assert cash >= money(snapshot.equity * D('0.1'))


def test_projected_sales_fund_buys_only_after_deducting_both_fees():
    snapshot = account('0', {'OLD': (5, '100')})
    orders = rebalance_orders([target('NEW', '1')], snapshot, {'OLD': D('100'), 'NEW': D('100')}, AS_OF, KEY,
                              cash_buffer=ZERO, execution_buffer_bps=ZERO)
    assert legs(orders) == [('OLD', 'SELL', 5), ('NEW', 'BUY', 4)]
    cash, quantities = settle(snapshot, orders, bps=ZERO)
    assert cash == D('98')
    assert quantities == {'OLD': 0, 'NEW': 4}


def test_empty_targets_liquidate_removed_holdings_without_shorting():
    snapshot = account('20', {'BBB': (1, '50'), 'AAA': (3, '100')})
    orders = rebalance_orders([], snapshot, {'AAA': D('100'), 'BBB': D('50')}, AS_OF, KEY)
    assert legs(orders) == [('AAA', 'SELL', 3), ('BBB', 'SELL', 1)]
    cash, quantities = settle(snapshot, orders)
    assert cash > snapshot.cash
    assert quantities == {'AAA': 0, 'BBB': 0}


def test_unchanged_target_and_zero_holdings_make_no_orders():
    snapshot = account('500', {'AAA': (5, '100'), 'EMPTY': (0, '1')})
    assert rebalance_orders([target('AAA', '0.5'), target('ZERO', '0')], snapshot, {'AAA': D('100')}, AS_OF, KEY) == []
    assert rebalance_orders([], account('0'), {}, AS_OF, KEY) == []


def test_fee_dominated_sale_is_omitted_when_it_would_borrow():
    snapshot = account('0', {'DUST': (1, '0.1')})
    assert rebalance_orders([], snapshot, {'DUST': D('0.1')}, AS_OF, KEY) == []


def test_cash_generating_sells_can_fund_fee_dominated_sells():
    snapshot = account('0', {'AAA': (1, '0.1'), 'ZZZ': (1, '100')})
    orders = rebalance_orders([], snapshot, {'AAA': D('0.1'), 'ZZZ': D('100')}, AS_OF, KEY)
    assert legs(orders) == [('ZZZ', 'SELL', 1), ('AAA', 'SELL', 1)]
    _, quantities = settle(snapshot, orders)
    assert quantities == {'AAA': 0, 'ZZZ': 0}


def test_buy_reservation_uses_rounded_execution_price_before_notional():
    snapshot = account('1116.7667')
    orders = rebalance_orders([target('AAA', '1')], snapshot, {'AAA': D('1.1111')}, AS_OF, KEY, cash_buffer=ZERO)
    # 1000 * money(1.1111 * 1.0006) + 5 = 1116.8000, not 1116.7667.
    assert legs(orders) == [('AAA', 'BUY', 999)]
    settle(snapshot, orders)


def test_sell_proceeds_use_rounded_execution_price_before_funding_buys():
    snapshot = account('0', {'OLD': (1000, '1.1111')})
    orders = rebalance_orders([target('NEW', '1')], snapshot, {'OLD': D('1.1111'), 'NEW': D('55.4882')}, AS_OF, KEY,
                              cash_buffer=ZERO, fee_per_share=ZERO, minimum_fee=ZERO)
    # Sale proceeds are 1110.4000; twenty new shares would cost 1110.4300.
    assert legs(orders) == [('OLD', 'SELL', 1000), ('NEW', 'BUY', 19)]
    settle(snapshot, orders, per_share=ZERO, minimum=ZERO)


def test_sale_rounding_cannot_hide_a_fee_cash_deficit():
    snapshot = account('0', {'DUST': (5, '0.1234')})
    orders = rebalance_orders([], snapshot, {'DUST': D('0.1234')}, AS_OF, KEY,
                              fee_per_share=ZERO, minimum_fee=D('0.6166'))
    assert orders == []


def test_reference_price_is_executable_quote_not_position_cost():
    snapshot = account('800', {'AAA': (2, '100')})
    snapshot.positions['AAA'].average_cost = D('7')
    orders = rebalance_orders([target('AAA', '0.5')], snapshot, {'AAA': D('125')}, AS_OF, KEY)
    assert legs(orders) == [('AAA', 'BUY', 2)]
    assert orders[0].reference_price == D('125')


def test_stable_sha_ids_ignore_retry_time_price_and_quantity_but_not_key():
    first = rebalance_orders([target('AAA', '0.5')], account(), {'AAA': D('100')}, AS_OF, KEY)[0]
    retried = rebalance_orders([target('AAA', '0.4')], account(), {'AAA': D('110')}, AS_OF + timedelta(minutes=1), KEY)[0]
    assert first.quantity != retried.quantity
    assert first.reference_price != retried.reference_price
    assert first.created_at != retried.created_at
    assert (first.intent_id, first.client_order_id) == (retried.intent_id, retried.client_order_id)
    payload = json.dumps(['quantlab.portfolio.v1', KEY, 'AAA', 'BUY'], separators=(',', ':'))
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    assert first.intent_id == f'portfolio-{digest}'
    assert first.client_order_id == f'ql-{digest[:32]}'
    other = rebalance_orders([target('AAA', '0.5')], account(), {'AAA': D('100')}, AS_OF, KEY + ':next')[0]
    assert first.intent_id != other.intent_id
    assert first.client_order_id != other.client_order_id


def test_ids_distinguish_symbol_and_side():
    buys = rebalance_orders([target('AAA', '0.3'), target('BBB', '0.3')], account(),
                            {'AAA': D('100'), 'BBB': D('100')}, AS_OF, KEY)
    sell = rebalance_orders([], account('500', {'AAA': (5, '100')}), {'AAA': D('100')}, AS_OF, KEY)
    assert len({item.intent_id for item in buys + sell}) == 3
    assert len({item.client_order_id for item in buys + sell}) == 3


def test_ids_are_identical_across_processes_and_python_hash_seeds():
    code = """
import json
from datetime import datetime, timezone
from decimal import Decimal as D
from quantlab.models import AccountSnapshot, TargetWeight
from quantlab.portfolio import rebalance_orders
at = datetime(2024, 1, 5, 22, tzinfo=timezone.utc)
account = AccountSnapshot(D('1000'), D('1000'), {}, at)
order = rebalance_orders([TargetWeight('AAA', D('0.5'), 'test')], account, {'AAA': D('100')}, at, 'stable')[0]
print(json.dumps([order.intent_id, order.client_order_id]))
"""
    outputs = [subprocess.check_output(
        [sys.executable, '-c', code], text=True, timeout=10,
        env={**os.environ, 'PYTHONHASHSEED': seed, 'PYTHONDONTWRITEBYTECODE': '1'},
    ) for seed in ('1', '42')]
    assert outputs[0] == outputs[1]
    assert all(json.loads(outputs[0]))


def test_inputs_unchanged_and_order_independent_with_equal_duplicates():
    snapshot = account('1000', {'ZZZ': (2, '100'), 'YYY': (2, '100')})
    targets = [target('BBB', '0.3'), target('AAA', '0.3'), target('AAA', '0.3')]
    prices = {'ZZZ': D('100'), 'YYY': D('100'), 'BBB': D('100'), 'AAA': D('100')}
    before = deepcopy((snapshot, targets, prices))
    expected = rebalance_orders(targets, snapshot, prices, AS_OF, KEY)
    reversed_account = replace(snapshot, positions=dict(reversed(list(snapshot.positions.items()))))
    assert rebalance_orders(iter(reversed(targets)), reversed_account, dict(reversed(list(prices.items()))), AS_OF, KEY) == expected
    assert (snapshot, targets, prices) == before
    assert len({(item.symbol, item.side) for item in expected}) == len(expected)


@pytest.mark.parametrize('bad_targets', [
    [target('AAA', '-0.1')], [target('AAA', '1.1')], [target('AAA', 'NaN')],
    [target('AAA', '0.6'), target('BBB', '0.5')],
    [target('AAA', '0.3'), target('AAA', '0.4')], [target(' AAA', '0.1')],
])
def test_invalid_targets_fail_closed(bad_targets):
    with pytest.raises(ValueError):
        rebalance_orders(bad_targets, account(), {'AAA': D('100'), 'BBB': D('100')}, AS_OF, KEY)


@pytest.mark.parametrize('prices', [{}, {'AAA': D('0')}, {'AAA': D('-1')}, {'AAA': D('NaN')}, {'AAA': D('Infinity')}])
def test_required_executable_quotes_must_be_present_positive_and_finite(prices):
    with pytest.raises(ValueError):
        rebalance_orders([target('AAA', '0.3')], account(), prices, AS_OF, KEY)
    with pytest.raises(ValueError):
        rebalance_orders([], account('0', {'AAA': (2, '100')}), prices, AS_OF, KEY)


@pytest.mark.parametrize('quantity', [-1, True, 1.5])
def test_negative_or_fractional_holdings_are_rejected(quantity):
    snapshot = account('100', {'AAA': (1, '100')})
    snapshot.positions['AAA'].quantity = quantity
    with pytest.raises(ValueError, match='nonnegative whole shares'):
        rebalance_orders([], snapshot, {'AAA': D('100')}, AS_OF, KEY)


@pytest.mark.parametrize('change', [
    {'as_of': AS_OF + timedelta(seconds=1)}, {'as_of': AS_OF.replace(tzinfo=None)},
    {'reconciled': False}, {'cash': D('-1')}, {'cash': D('NaN')},
    {'equity': D('-1')}, {'cash': D('1001')},
])
def test_unsafe_account_snapshots_are_rejected(change):
    with pytest.raises(ValueError):
        rebalance_orders([], replace(account(), **change), {}, AS_OF, KEY)


def test_mismatched_position_symbol_is_rejected():
    snapshot = account('100', {'AAA': (1, '100')})
    snapshot.positions['AAA'].symbol = 'BBB'
    with pytest.raises(ValueError, match='position key'):
        rebalance_orders([], snapshot, {'AAA': D('100')}, AS_OF, KEY)


@pytest.mark.parametrize('options', [
    {'cash_buffer': D('-0.1')}, {'cash_buffer': D('1.1')},
    {'fee_per_share': D('-1')}, {'minimum_fee': D('-1')}, {'minimum_fee': D('NaN')},
    {'execution_buffer_bps': D('-1')}, {'execution_buffer_bps': D('10000')},
])
def test_invalid_cost_assumptions_are_rejected(options):
    with pytest.raises(ValueError):
        rebalance_orders([], account(), {}, AS_OF, KEY, **options)


@pytest.mark.parametrize('key', ['', '   ', None, 123])
def test_durable_nonempty_rebalance_key_is_required(key):
    with pytest.raises(ValueError, match='rebalance_key'):
        rebalance_orders([], account(), {}, AS_OF, key)


def test_planning_time_must_be_utc():
    with pytest.raises(ValueError, match='UTC'):
        rebalance_orders([], account(), {}, AS_OF.replace(tzinfo=None), KEY)
