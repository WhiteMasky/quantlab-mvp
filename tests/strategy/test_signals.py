from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
import inspect
import math
import random

import pytest

import quantlab.strategy as strategy_module
from quantlab.models import MarketBar, Signal
from quantlab.strategy import STRATEGY_LIMITATIONS, generate_signals


UTC = timezone.utc


def trading_day(index):
    day = date(2024, 1, 2)
    while index:
        day += timedelta(days=1)
        if day.weekday() < 5:
            index -= 1
    return day


def bar(symbol, index, price='100', *, available_at=None):
    session = trading_day(index)
    close_at = datetime.combine(session, time(21), UTC)
    price = Decimal(str(price))
    return MarketBar(
        symbol=symbol, session=session,
        open_at=datetime.combine(session, time(14, 30), UTC),
        close_at=close_at,
        available_at=available_at or close_at,
        open=price, high=price, low=price, close=price,
        adjusted_close=price, volume=1000,
    )


def series(symbol, prices):
    return [bar(symbol, index, price) for index, price in enumerate(prices)]


def by_symbol(signals):
    return {signal.symbol: signal for signal in signals}


def selected(signals):
    return [signal.symbol for signal in signals if signal.eligible]


def test_public_signature_and_exports():
    signature = inspect.signature(generate_signals)
    assert list(signature.parameters) == ['bars', 'as_of', 'strategy', 'lookback', 'skip', 'top_n']
    assert [p.default for p in list(signature.parameters.values())[2:]] == ['trend', 63, 5, 2]
    assert 'generate_signals' in strategy_module.__all__
    assert all(isinstance(value, str) and value for value in STRATEGY_LIMITATIONS)


def test_default_window_requires_64_observations():
    bars = series('AAA', range(100, 164))
    warmup = generate_signals(bars[:-1], bars[-2].close_at)
    ready = generate_signals(bars, bars[-1].close_at)
    assert not warmup[0].eligible
    assert warmup[0].score == 0.0
    assert 'need 64' in warmup[0].reason
    assert ready[0].eligible
    assert ready[0].score == pytest.approx(0.63)


def test_trend_is_time_series_return_and_does_not_limit_positive_symbols():
    bars = []
    for symbol in ('CCC', 'AAA', 'BBB'):
        bars += series(symbol, ['100', '105', '110', '120'])
    bars += series('NEG', ['100', '99', '95', '80'])
    bars += series('FLAT', ['100', '99', '105', '100'])
    result = generate_signals(bars, bars[-1].close_at, lookback=3, top_n=1)
    signals = by_symbol(result)
    assert selected(result) == ['AAA', 'BBB', 'CCC']
    assert signals['AAA'].score == pytest.approx(0.2)
    assert signals['NEG'].score == pytest.approx(-0.2)
    assert signals['FLAT'].score == 0.0
    assert 'non-positive trend' in signals['NEG'].reason
    assert 'cash' in signals['FLAT'].reason
    assert 'not an order' in signals['AAA'].reason
    assert all(isinstance(signal, Signal) for signal in result)
    assert all(type(signal.eligible) is bool for signal in result)
    assert result == generate_signals(bars, bars[-1].close_at, lookback=3, skip=0, top_n=100)


def test_momentum_skip_shortens_horizon_without_shifting_start():
    bars = series('AAA', ['100', '110', '120', '160', '10'])
    result = generate_signals(bars, bars[-1].close_at, 'momentum', lookback=4, skip=2)
    assert result[0].eligible
    assert result[0].score == pytest.approx(0.2)
    assert 'skip=2' in result[0].reason
    assert 'rank=1/1' in result[0].reason
    unskipped = generate_signals(bars, bars[-1].close_at, 'momentum', lookback=4, skip=0)
    assert unskipped[0].score == pytest.approx(-0.9)
    assert not unskipped[0].eligible


def test_momentum_ignores_values_inside_skipped_tail():
    bars = series('AAA', [100, 101, 115, 90, 80])
    changed = [replace(item, adjusted_close=Decimal('999999')) if index >= 3 else item
               for index, item in enumerate(bars)]
    params = dict(strategy='momentum', lookback=4, skip=2)
    assert generate_signals(bars, bars[-1].close_at, **params) == generate_signals(
        changed, bars[-1].close_at, **params
    )


def test_momentum_stable_lexical_ties_and_positive_only_top_n():
    bars = []
    for symbol, last in [('ZZZ', 120), ('BBB', 120), ('AAA', 120), ('BEST', 140),
                         ('ZERO', 100), ('NEG', 80)]:
        bars += series(symbol, [100, 110, last])
    result = generate_signals(bars, bars[-1].close_at, 'momentum', 2, 0, 2)
    signals = by_symbol(result)
    assert selected(result) == ['AAA', 'BEST']
    assert [item.symbol for item in result] == sorted(signals)
    assert 'rank=1/4' in signals['BEST'].reason
    assert 'rank=2/4' in signals['AAA'].reason
    assert 'outside top_n' in signals['BBB'].reason
    assert not signals['ZERO'].eligible
    assert not signals['NEG'].eligible
    assert 'not ranked' in signals['NEG'].reason
    all_positive = generate_signals(bars, bars[-1].close_at, 'momentum', 2, 0, 99)
    assert selected(all_positive) == ['AAA', 'BBB', 'BEST', 'ZZZ']


@pytest.mark.parametrize('rule', ['trend', 'momentum'])
def test_same_parameters_reproduce_signals_for_shuffled_and_generator_inputs(rule):
    bars = series('ZZZ', [100, 105, 120, 130, 125]) + series('AAA', [100, 105, 120, 130, 125])
    snapshot = list(bars)
    params = dict(strategy=rule, lookback=3, skip=1, top_n=1)
    expected = generate_signals(bars, bars[-1].close_at, **params)
    for seed in range(8):
        shuffled = list(bars)
        random.Random(seed).shuffle(shuffled)
        assert generate_signals(shuffled, bars[-1].close_at, **params) == expected
        assert generate_signals(iter(shuffled), bars[-1].close_at, **params) == expected
    assert bars == snapshot


@pytest.mark.parametrize('rule', ['trend', 'momentum'])
def test_decimal_context_does_not_change_scores_rank_or_reason(rule):
    bars = series('AAA', ['100', '110', '120.123456781'])
    bars += series('ZZZ', ['100', '110', '120.123456789'])
    params = dict(strategy=rule, lookback=2, skip=0, top_n=1)
    expected = generate_signals(bars, bars[-1].close_at, **params)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        assert generate_signals(bars, bars[-1].close_at, **params) == expected
    if rule == 'momentum':
        assert selected(expected) == ['ZZZ']


@pytest.mark.parametrize('rule', ['trend', 'momentum'])
def test_future_appends_and_future_price_changes_cannot_change_past_signals(rule):
    history = series('AAA', [100, 105, 103, 110, 120, 118, 130, 125])
    history += series('BBB', [100, 105, 108, 103, 115, 120, 125, 130])
    params = dict(strategy=rule, lookback=3, skip=1, top_n=1)
    for day_index in range(8):
        as_of = bar('CLOCK', day_index).close_at
        past = [item for item in history if item.close_at <= as_of]
        future = [replace(item, open=Decimal('999'), high=Decimal('999'),
                          low=Decimal('999'), close=Decimal('999'),
                          adjusted_close=Decimal('999'))
                  for item in history if item.close_at > as_of]
        future += [bar('FUTURE_ONLY', 8, '9000')]
        expected = generate_signals(past, as_of, **params)
        assert generate_signals(history, as_of, **params) == expected
        assert generate_signals(past + future, as_of, **params) == expected
        assert 'FUTURE_ONLY' not in by_symbol(expected)


@pytest.mark.parametrize('rule', ['trend', 'momentum'])
def test_available_at_and_close_at_are_both_inclusive_gates(rule):
    bars = series('AAA', [100, 110, 120])
    as_of = bars[-1].close_at
    params = dict(strategy=rule, lookback=2, skip=0)
    assert generate_signals(bars, as_of, **params)[0].eligible
    assert not generate_signals(bars, as_of - timedelta(microseconds=1), **params)[0].eligible

    delayed = bars[:-1] + [replace(bars[-1], available_at=as_of + timedelta(seconds=1))]
    assert not generate_signals(delayed, as_of, **params)[0].eligible
    assert generate_signals(delayed, as_of + timedelta(seconds=1), **params)[0].eligible

    early_publication = bars + [replace(bar('AAA', 3, '1'), available_at=as_of)]
    assert generate_signals(early_publication, as_of, **params) == generate_signals(bars, as_of, **params)


def test_unavailable_prices_and_symbol_are_not_validated_or_exposed():
    bars = series('AAA', [100, 110, 120])
    as_of = bars[-1].close_at
    hidden = replace(bar('FUTURE', 3), adjusted_close=Decimal('NaN'), volume=-1)
    not_closed = replace(hidden, available_at=as_of)
    not_available = replace(bars[-1], symbol='HIDDEN', available_at=as_of + timedelta(days=1),
                            close=Decimal('NaN'))
    assert generate_signals(bars + [hidden, not_closed, not_available], as_of, lookback=2) == (
        generate_signals(bars, as_of, lookback=2)
    )


@pytest.mark.parametrize('rule', ['trend', 'momentum'])
def test_late_intermediate_observation_means_cash_until_it_is_known(rule):
    a = series('AAA', [100, 110, 120, 130])
    b = series('BBB', [100, 102, 104, 106])
    as_of = a[-1].close_at
    release = as_of + timedelta(minutes=5)
    a[1] = replace(a[1], available_at=release)
    params = dict(strategy=rule, lookback=3, skip=1, top_n=1)
    before = by_symbol(generate_signals(a + b, as_of, **params))
    assert not before['AAA'].eligible
    assert before['AAA'].score == 0.0
    assert 'have 3' in before['AAA'].reason
    assert before['BBB'].eligible
    after = by_symbol(generate_signals(a + b, release, **params))
    assert after['AAA'].eligible
    assert after['AAA'].score == pytest.approx(0.3 if rule == 'trend' else 0.2)


@pytest.mark.parametrize('rule', ['trend', 'momentum'])
def test_cross_section_requires_common_window_and_no_stale_or_new_symbol_padding(rule):
    full = series('FULL', [100, 105, 110, 115, 120])
    stale = series('STALE', [100, 150, 200, 250])
    new = [bar('NEW', 3, 100), bar('NEW', 4, 200)]
    result = by_symbol(generate_signals(full + stale + new, full[-1].close_at, rule, 3, 1))
    assert result['FULL'].eligible
    assert not result['STALE'].eligible
    assert not result['NEW'].eligible
    assert 'incomplete_window' in result['STALE'].reason
    assert result['STALE'].score == result['NEW'].score == 0.0


@pytest.mark.parametrize('rule', ['trend', 'momentum'])
def test_history_older_than_window_does_not_change_return(rule):
    bars = series('AAA', [1, 3, 100, 105, 110, 120])
    params = dict(strategy=rule, lookback=3, skip=1)
    assert generate_signals(bars, bars[-1].close_at, **params) == generate_signals(
        bars[2:], bars[-1].close_at, **params
    )


@pytest.mark.parametrize('rule', ['trend', 'momentum'])
@pytest.mark.parametrize('count', [1, 2, 3])
def test_warmup_is_finite_and_never_eligible(rule, count):
    bars = series('AAA', range(100, 100 + count))
    result = generate_signals(bars, bars[-1].close_at, rule, 3, 1)
    assert len(result) == 1
    assert result[0].score == 0.0
    assert math.isfinite(result[0].score)
    assert result[0].eligible is False
    assert 'warmup' in result[0].reason
    assert 'cash' in result[0].reason


def test_empty_and_fully_unavailable_inputs_have_no_discovered_symbols():
    future = bar('AAA', 2)
    assert generate_signals([], future.close_at) == []
    assert generate_signals([future], future.open_at) == []


def test_one_interval_is_valid_with_no_skip():
    bars = series('AAA', [100, 110])
    for rule in ['trend', 'momentum']:
        result = generate_signals(bars, bars[-1].close_at, rule, 1, 0, 1)
        assert result[0].score == pytest.approx(0.1)
        assert result[0].eligible


def test_duplicate_observations_are_idempotent_and_conflicts_fail_closed():
    bars = series('AAA', [100, 110, 120])
    params = dict(lookback=2)
    expected = generate_signals(bars, bars[-1].close_at, **params)
    assert generate_signals(bars + bars, bars[-1].close_at, **params) == expected
    changed = replace(bars[-1], adjusted_close=Decimal('150'))
    with pytest.raises(ValueError, match='conflicting bars'):
        generate_signals(bars + [changed], bars[-1].close_at, **params)
    delayed_revision = replace(changed, available_at=bars[-1].close_at + timedelta(seconds=1))
    assert generate_signals(bars + [delayed_revision], bars[-1].close_at, **params) == expected


def test_only_adjusted_close_drives_research_return_not_executable_close():
    bars = series('AAA', [100, 90, 50])
    bars[-1] = replace(bars[-1], adjusted_close=Decimal('120'))
    result = generate_signals(bars, bars[-1].close_at, lookback=2)
    assert result[0].eligible
    assert result[0].score == pytest.approx(0.2)


def test_point_in_time_limitation_is_explicit_and_overwritten_old_prices_are_not_magic():
    disclosure = ' '.join(STRATEGY_LIMITATIONS).lower()
    assert 'point-in-time' in disclosure
    assert 'look-ahead bias' in disclosure
    assert 'survivorship' in disclosure
    assert 'synthetic' in disclosure
    bars = series('AAA', [100, 110, 120])
    past_result = generate_signals(bars, bars[-1].close_at, lookback=2)
    revised = [replace(bars[0], adjusted_close=Decimal('150'))] + bars[1:]
    assert generate_signals(revised, bars[-1].close_at, lookback=2) != past_result


@pytest.mark.parametrize('parameters', [
    {'strategy': 'unknown'}, {'strategy': 'Trend'}, {'strategy': None}, {'strategy': []},
    {'lookback': 0}, {'lookback': -1}, {'lookback': 1.5}, {'lookback': True},
    {'lookback': '63'}, {'lookback': Decimal('63')}, {'lookback': None},
    {'skip': -1}, {'skip': True}, {'skip': 1.5}, {'skip': '5'}, {'skip': None},
    {'top_n': 0}, {'top_n': -1}, {'top_n': False}, {'top_n': 2.0}, {'top_n': None},
    {'strategy': 'momentum', 'lookback': 5, 'skip': 5},
    {'strategy': 'momentum', 'lookback': 3, 'skip': 4},
])
def test_invalid_parameters_rejected_even_with_empty_history(parameters):
    with pytest.raises(ValueError):
        generate_signals([], bar('AAA', 0).close_at, **parameters)


@pytest.mark.parametrize('as_of', [
    None, '2024-01-02', date(2024, 1, 2), datetime(2024, 1, 2),
    datetime(2024, 1, 2, tzinfo=timezone(timedelta(hours=1))),
])
def test_as_of_requires_utc_aware_datetime(as_of):
    with pytest.raises(ValueError, match='as_of'):
        generate_signals([], as_of)


@pytest.mark.parametrize('bars', [None, 1, [None], [{}], ['AAA']])
def test_wrong_input_type_rejected(bars):
    with pytest.raises(ValueError, match='bars'):
        generate_signals(bars, bar('AAA', 0).close_at)


@pytest.mark.parametrize('field', ['open', 'high', 'low', 'close', 'adjusted_close'])
@pytest.mark.parametrize('bad_price', [
    Decimal('NaN'), Decimal('sNaN'), Decimal('Infinity'), Decimal('-Infinity'),
    Decimal('0'), Decimal('-1'), 100.0, '100', 100,
])
def test_invalid_known_prices_rejected_before_signal_math(field, bad_price):
    bars = series('AAA', [100, 110, 120])
    bars[1] = replace(bars[1], **{field: bad_price})
    with pytest.raises(ValueError, match=field):
        generate_signals(bars, bars[-1].close_at, lookback=2)


@pytest.mark.parametrize('changes, message', [
    ({'symbol': ''}, 'symbol'), ({'symbol': ' AAA'}, 'symbol'), ({'symbol': None}, 'symbol'),
    ({'session': '2024-01-02'}, 'session'),
    ({'session': datetime(2024, 1, 2)}, 'session'),
    ({'session': date(2024, 1, 3)}, 'session'),
    ({'open_at': datetime(2024, 1, 2, 21, tzinfo=UTC)}, 'open_at'),
    ({'low': Decimal('101')}, 'OHLC'), ({'high': Decimal('99')}, 'OHLC'),
    ({'volume': -1}, 'volume'), ({'volume': True}, 'volume'), ({'volume': 100.0}, 'volume'),
])
def test_malformed_visible_data_fail_closed(changes, message):
    observation = replace(bar('AAA', 0), **changes)
    with pytest.raises(ValueError, match=message):
        generate_signals([observation], observation.close_at)


@pytest.mark.parametrize('field', ['open_at', 'close_at', 'available_at'])
@pytest.mark.parametrize('timestamp', [None, datetime(2024, 1, 2),
                                       datetime(2024, 1, 2, tzinfo=timezone(timedelta(hours=1)))])
def test_corrupt_timestamp_metadata_cannot_bypass_utc_gate(field, timestamp):
    observation = bar('AAA', 0)
    as_of = observation.close_at
    # Simulate a malformed deserializer bypassing the frozen model constructor.
    object.__setattr__(observation, field, timestamp)
    with pytest.raises(ValueError, match=field):
        generate_signals([observation], as_of)


@pytest.mark.parametrize('end', ['1e309', '1e1000000'])
def test_extreme_finite_prices_cannot_emit_infinite_scores(end):
    bars = series('AAA', [1, 1])
    bars[-1] = replace(bars[-1], adjusted_close=Decimal(end))
    with pytest.raises(ValueError, match='finite score'):
        generate_signals(bars, bars[-1].close_at, lookback=1)


def test_zero_volume_is_valid_but_not_order_authorization():
    bars = [replace(item, volume=0) for item in series('AAA', [100, 110])]
    result = generate_signals(bars, bars[-1].close_at, lookback=1)
    assert result[0].eligible
    assert 'not an order' in result[0].reason


def test_strategy_has_no_broker_or_llm_decision_dependency():
    source = ast.parse(inspect.getsource(strategy_module))
    imports = []
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or '')
    forbidden = ('quantlab.broker', 'ibapi', 'openai', 'anthropic', 'langchain')
    assert not any(name == prefix or name.startswith(prefix + '.')
                   for name in imports for prefix in forbidden)
