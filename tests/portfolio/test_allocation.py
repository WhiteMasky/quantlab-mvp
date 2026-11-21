from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, localcontext
from inspect import signature

import pytest

import quantlab.portfolio as portfolio
from quantlab.models import MarketBar, Signal, TargetWeight
from quantlab.portfolio import allocate, rebalance_orders

D = Decimal
ZERO = D('0')
AS_OF = datetime(2024, 1, 5, 22, tzinfo=timezone.utc)
LOW = ('-0.01', '0', '0.01')
HIGH = ('0.02', '0', '-0.02')


def history(symbol, returns=LOW, sessions=None):
    """Exact, local adjusted-price histories; never an investment fixture."""
    sessions = sessions or [date(2024, 1, 2) + timedelta(days=i) for i in range(len(returns) + 1)]
    with localcontext() as context:
        context.prec = 50
        prices = [D('100')]
        for value in returns:
            prices.append(prices[-1] * (D('1') + D(value)))
    return [
        MarketBar(
            symbol=symbol,
            session=day,
            open_at=datetime.combine(day, time(14, 30), timezone.utc),
            close_at=datetime.combine(day, time(21), timezone.utc),
            available_at=datetime.combine(day, time(21, 1), timezone.utc),
            open=price, high=price, low=price, close=price,
            adjusted_close=price, volume=10000,
        )
        for day, price in zip(sessions, prices)
    ]


def signal(symbol, score=1.0, eligible=True, at=AS_OF):
    return Signal(symbol, at, score, eligible, 'unit-test candidate')


def weights(targets):
    assert all(isinstance(target, TargetWeight) for target in targets)
    assert all(isinstance(target.weight, Decimal) and target.weight > 0 for target in targets)
    assert len({target.symbol for target in targets}) == len(targets)
    assert [target.symbol for target in targets] == sorted(target.symbol for target in targets)
    return {target.symbol: target.weight for target in targets}


def gross_turnover(before, after):
    return sum((abs(after.get(symbol, ZERO) - before.get(symbol, ZERO)) for symbol in before.keys() | after.keys()), ZERO)


@pytest.fixture
def pair():
    return [signal('AAA'), signal('BBB')], history('AAA') + history('BBB', HIGH)


def test_public_exports_and_contract_signatures():
    assert portfolio.__all__ == ['allocate', 'rebalance_orders']
    allocation = signature(allocate)
    assert list(allocation.parameters) == [
        'signals', 'bars', 'as_of', 'max_weight', 'cash_buffer', 'vol_lookback',
        'max_correlation', 'current_weights', 'turnover_limit',
    ]
    assert [item.default for item in list(allocation.parameters.values())[3:]] == [
        D('0.35'), D('0.10'), 20, 0.90, None, D('0.30'),
    ]
    orders = signature(rebalance_orders)
    assert list(orders.parameters)[:5] == ['targets', 'account', 'prices', 'as_of', 'rebalance_key']
    assert all(item.default is item.empty for item in list(orders.parameters.values())[:5])
    assert all(item.kind is item.KEYWORD_ONLY for item in list(orders.parameters.values())[5:])


def test_inverse_volatility_decimal_weights_and_cash_buffer(pair):
    signals, bars = pair
    result = weights(allocate(signals, bars, AS_OF, max_weight=D('0.8'), vol_lookback=3))
    # Sample volatility is exactly 0.01 and 0.02, so weights have a 2:1 ratio.
    assert result == {'AAA': D('0.6'), 'BBB': D('0.3')}
    assert D('1') - sum(result.values()) == D('0.1')


@pytest.mark.parametrize(('cap', 'expected'), [
    ('0.5', {'AAA': D('0.5'), 'BBB': D('0.4')}),
    ('0.35', {'AAA': D('0.35'), 'BBB': D('0.35')}),
    ('0', {}),
])
def test_caps_redistribute_without_forcing_investment(pair, cap, expected):
    signals, bars = pair
    result = weights(allocate(signals, bars, AS_OF, max_weight=D(cap), vol_lookback=3))
    assert result == expected
    assert sum(result.values(), ZERO) <= D('0.9')
    assert all(value <= D(cap) for value in result.values())


def test_full_cash_buffer_disables_allocation(pair):
    signals, bars = pair
    assert allocate(signals, bars, AS_OF, cash_buffer=D('1'), vol_lookback=3) == []


def test_correlation_is_positive_not_absolute_and_ties_are_lexical():
    bars = history('AAA') + history('BBB') + history('CCC', HIGH)
    signals = [signal('CCC'), signal('BBB'), signal('AAA')]
    result = weights(allocate(signals, bars, AS_OF, vol_lookback=3))
    # AAA/BBB are perfectly correlated; CCC is perfectly anticorrelated.
    assert result == {'AAA': D('0.35'), 'CCC': D('0.35')}
    stronger = [replace(item, score=2.0) if item.symbol == 'BBB' else item for item in signals]
    assert set(weights(allocate(stronger, bars, AS_OF, vol_lookback=3))) == {'BBB', 'CCC'}
    assert set(weights(allocate(signals, bars, AS_OF, vol_lookback=3, max_correlation=D('1')))) == {'AAA', 'BBB', 'CCC'}
    assert set(weights(allocate(signals, bars, AS_OF, vol_lookback=3, max_correlation=D('-1')))) == {'AAA', 'CCC'}


def test_pair_correlation_requires_matching_return_endpoints():
    days = [date(2024, 1, day) for day in (1, 3, 4, 5)]
    bars = history('AAA') + history('BBB', HIGH, sessions=days)
    # Same final date and observation count do not make the return intervals equal.
    assert weights(allocate([signal('BBB'), signal('AAA')], bars, AS_OF, vol_lookback=3)) == {'AAA': D('0.35')}


@pytest.mark.parametrize('returns', [(), ('0.01', '0'), ('0', '0', '0'), ('0.1', '0.1', '0.1')])
def test_missing_short_or_zero_volatility_history_stays_cash(returns):
    bars = history('AAA', returns)
    assert allocate([signal('AAA')], bars, AS_OF, vol_lookback=3) == []
    assert allocate([signal('AAA')], [], AS_OF, vol_lookback=3) == []


@pytest.mark.parametrize('price', [D('0'), D('-1'), D('NaN'), D('Infinity')])
def test_invalid_adjusted_prices_do_not_receive_weights(price):
    bars = history('AAA')
    bars[-1] = replace(bars[-1], adjusted_close=price)
    assert allocate([signal('AAA')], bars, AS_OF, vol_lookback=3) == []


def test_volatility_uses_adjusted_prices_not_executable_raw_prices(pair):
    signals, bars = pair
    raw_changed = [replace(bar, open=D('7'), high=D('7'), low=D('7'), close=D('7')) for bar in bars]
    assert allocate(signals, raw_changed, AS_OF, vol_lookback=3) == allocate(signals, bars, AS_OF, vol_lookback=3)


@pytest.mark.parametrize('timestamp', ['available_at', 'close_at', 'open_at'])
def test_unavailable_bars_are_filtered_before_revisions_and_estimation(pair, timestamp):
    signals, bars = pair
    future = replace(bars[3], adjusted_close=D('NaN'), **{timestamp: AS_OF + timedelta(days=1)})
    expected = allocate(signals, bars, AS_OF, vol_lookback=3)
    assert allocate(signals, [future, *reversed(bars)], AS_OF, vol_lookback=3) == expected


def test_future_sessions_never_enter_the_rolling_window(pair):
    signals, bars = pair
    future = history('AAA', ('0.9', '-0.8', '0.5'), sessions=[date(2024, 1, day) for day in (8, 9, 10, 11)])
    assert allocate(signals, bars + future, AS_OF, vol_lookback=3) == allocate(signals, bars, AS_OF, vol_lookback=3)


def test_latest_known_revision_wins_and_equal_time_conflicts_fail_closed(pair):
    signals, bars = pair
    revised = replace(bars[3], adjusted_close=D('101'), available_at=AS_OF)
    expected = allocate(signals, [*bars[:3], revised, *bars[4:]], AS_OF, vol_lookback=3)
    assert allocate(signals, [revised, *bars], AS_OF, vol_lookback=3) == expected
    assert allocate(signals, [*bars, revised], AS_OF, vol_lookback=3) == expected
    with pytest.raises(ValueError, match='Ambiguous known bar'):
        allocate(signals, [*bars, replace(bars[3], adjusted_close=D('101'))], AS_OF, vol_lookback=3)
    assert allocate(signals, [*bars, bars[3]], AS_OF, vol_lookback=3) == allocate(signals, bars, AS_OF, vol_lookback=3)


def test_signal_availability_withdrawals_and_ties(pair):
    _, bars = pair
    earlier = AS_OF - timedelta(hours=1)
    signals = [signal('AAA', at=earlier), signal('AAA', eligible=False), signal('BBB')]
    expected = {'BBB': D('0.35')}
    assert weights(allocate(signals, bars, AS_OF, vol_lookback=3)) == expected
    assert weights(allocate(reversed(signals), reversed(bars), AS_OF, vol_lookback=3)) == expected
    assert weights(allocate([signal('AAA'), signal('AAA', eligible=False)], bars, AS_OF, vol_lookback=3)) == {}
    future = signal('AAA', score=100.0, at=AS_OF + timedelta(seconds=1))
    assert weights(allocate([future, signal('BBB')], bars, AS_OF, vol_lookback=3)) == expected
    # A future withdrawal must not erase a currently known candidate.
    future_withdrawal = replace(future, eligible=False)
    assert weights(allocate([signal('AAA'), future_withdrawal], bars, AS_OF, vol_lookback=3)) == {'AAA': D('0.35')}


def test_turnover_distinguishes_unknown_portfolio_from_known_all_cash(pair):
    signals, bars = pair
    options = {'max_weight': D('0.8'), 'vol_lookback': 3, 'turnover_limit': D('0.3')}
    unconstrained = weights(allocate(signals, bars, AS_OF, **options))
    constrained = weights(allocate(signals, bars, AS_OF, current_weights={}, **options))
    assert unconstrained == {'AAA': D('0.6'), 'BBB': D('0.3')}
    assert constrained == {'AAA': D('0.2'), 'BBB': D('0.1')}
    assert gross_turnover({}, constrained) == D('0.3')
    assert allocate(signals, bars, AS_OF, current_weights={}, turnover_limit=ZERO, vol_lookback=3) == []


@pytest.mark.parametrize(('limit', 'expected'), [
    ('0', {'OLD': D('0.2')}),
    ('0.1', {'OLD': D('0.1')}),
    ('0.3', {'AAA': D('0.1')}),
])
def test_removed_holdings_reduce_before_incompatible_additions(limit, expected):
    before = {'OLD': D('0.2')}
    result = weights(allocate(
        [signal('AAA')], history('AAA'), AS_OF, vol_lookback=3,
        current_weights=before, turnover_limit=D(limit),
    ))
    assert result == expected
    assert gross_turnover(before, result) <= D(limit)


def test_turnover_counts_both_sells_and_buys_not_half_the_sum(pair):
    signals, bars = pair
    before = {'AAA': D('0.2'), 'BBB': D('0.6')}
    result = weights(allocate(
        signals, bars, AS_OF, max_weight=D('0.8'), vol_lookback=3,
        current_weights=before, turnover_limit=D('0.4'),
    ))
    assert result == {'AAA': D('0.3'), 'BBB': D('0.3')}
    assert gross_turnover(before, result) == D('0.4')


def test_tight_turnover_is_rounded_down_not_exceeded(pair):
    signals, bars = pair
    limit = D('0.000000000002')
    result = weights(allocate(
        signals, bars, AS_OF, max_weight=D('0.8'), vol_lookback=3,
        current_weights={}, turnover_limit=limit,
    ))
    assert result == {'AAA': D('0.000000000001')}
    assert gross_turnover({}, result) <= limit


@pytest.mark.parametrize(('before', 'options'), [
    ({'AAA': D('0.7')}, {}),
    ({'AAA': D('0.8')}, {'max_weight': D('1'), 'cash_buffer': D('0.5')}),
    ({'AAA': D('0.2'), 'BBB': D('0.2')}, {}),
])
def test_infeasible_safety_reductions_raise_instead_of_violating_constraints(before, options):
    bars = history('AAA') + history('BBB')
    with pytest.raises(ValueError, match='turnover_limit cannot fund'):
        allocate([signal('AAA'), signal('BBB')], bars, AS_OF, vol_lookback=3,
                 current_weights=before, turnover_limit=D('0.1'), **options)


def test_feasible_safety_reductions_respect_cash_and_turnover():
    before = {'AAA': D('0.8')}
    result = weights(allocate(
        [signal('AAA')], history('AAA'), AS_OF, vol_lookback=3,
        max_weight=D('1'), cash_buffer=D('0.5'), current_weights=before, turnover_limit=D('0.3'),
    ))
    assert result == {'AAA': D('0.5')}
    assert gross_turnover(before, result) == D('0.3')


def test_no_candidates_only_reduces_existing_exposure():
    result = weights(allocate([], [], AS_OF, current_weights={'OLD': D('0.3')}, turnover_limit=D('0.1')))
    assert result == {'OLD': D('0.2')}
    assert allocate([], [], AS_OF) == []


def test_inputs_are_unchanged_and_iterable_order_is_irrelevant(pair):
    signals, bars = pair
    current = {'BBB': D('0.2'), 'AAA': D('0.3')}
    before = deepcopy((signals, bars, current))
    expected = allocate(signals, bars, AS_OF, current_weights=current, vol_lookback=3)
    assert allocate(iter(reversed(signals)), iter(reversed(bars)), AS_OF,
                    current_weights=dict(reversed(list(current.items()))), vol_lookback=3) == expected
    assert (signals, bars, current) == before


@pytest.mark.parametrize('options', [
    {'max_weight': D('-0.1')}, {'max_weight': D('1.1')}, {'max_weight': True},
    {'cash_buffer': D('-0.1')}, {'cash_buffer': D('1.1')}, {'cash_buffer': D('NaN')},
    {'turnover_limit': D('-0.1')}, {'turnover_limit': D('Infinity')},
    {'vol_lookback': 1}, {'vol_lookback': True}, {'vol_lookback': 2.5},
    {'max_correlation': D('-1.01')}, {'max_correlation': D('1.01')},
    {'max_correlation': float('nan')}, {'max_correlation': 'bad'},
    {'current_weights': []}, {'current_weights': {'AAA': D('-0.1')}},
    {'current_weights': {'AAA': D('0.6'), 'BBB': D('0.5')}},
    {'current_weights': {' AAA': D('0.1')}},
])
def test_invalid_allocation_parameters_fail_closed(options):
    with pytest.raises(ValueError):
        allocate([], [], AS_OF, **options)


@pytest.mark.parametrize('invalid', [
    signal(''), signal(' AAA'), signal('AAA', score=float('inf')),
    signal('AAA', eligible=1), signal('AAA', at=AS_OF.replace(tzinfo=None)),
])
def test_invalid_known_signals_fail_closed(invalid):
    with pytest.raises(ValueError):
        allocate([invalid], history('AAA'), AS_OF, vol_lookback=3)


def test_as_of_must_be_utc():
    with pytest.raises(ValueError, match='UTC'):
        allocate([], [], AS_OF.replace(tzinfo=None))
