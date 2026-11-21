"""Arithmetic oracles for metrics; no strategy or evaluator dependency."""
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import fmean, stdev
from zoneinfo import ZoneInfo

import pytest

from quantlab.models import EquityPoint, Fill
from quantlab.validation import performance_metrics

METRICS = {
    'total_return', 'annualized_return', 'max_drawdown', 'drawdown_duration',
    'volatility', 'sharpe', 'sortino', 'turnover', 'fee_ratio', 'market_exposure', 'sample_size',
}
START = datetime(2024, 1, 2, 21, tzinfo=timezone.utc)


def curve(equities, exposures=None):
    if exposures is None:
        exposures = [0] * len(equities)
    return [EquityPoint(START + timedelta(days=index), Decimal(str(equity)),
                        Decimal(str(equity)), Decimal(str(exposure)))
            for index, (equity, exposure) in enumerate(zip(equities, exposures))]


def fill(**changes):
    base = Fill('f1', 'o1', 'SPY', 'BUY', 2, Decimal('10'), Decimal('0.20'), START)
    return replace(base, **changes)


def assert_finite_or_explained(metrics):
    assert METRICS <= metrics.keys()
    for name in METRICS:
        value = metrics[name]
        if value is None:
            assert metrics['explanations'].get(name), name
        else:
            assert math.isfinite(value), name
    json.dumps(metrics, allow_nan=False)


def test_metrics_match_manual_daily_net_return_formulas():
    equities = [110, 99, 108.9, 108.9, 120, 96, 90]
    points = curve(equities, [value / 2 for value in equities])
    executions = [fill(), fill(fill_id='f2', side='SELL', quantity=1,
                               price=Decimal('12'), commission=Decimal('0.30'))]
    result = performance_metrics(points, executions, Decimal('100'))
    returns = [equities[index] / previous - 1
               for index, previous in enumerate([100, *equities[:-1]])]
    sigma = stdev(returns)
    downside = math.sqrt(sum(min(value, 0) ** 2 for value in returns) / len(returns))

    assert result['total_return'] == pytest.approx(-0.10)
    assert result['annualized_return'] == pytest.approx(0.9 ** (252 / 7) - 1)
    assert result['max_drawdown'] == pytest.approx(0.25)
    assert result['drawdown_duration'] == 3
    assert result['volatility'] == pytest.approx(sigma * math.sqrt(252))
    assert result['sharpe'] == pytest.approx(fmean(returns) / sigma * math.sqrt(252))
    assert result['sortino'] == pytest.approx(fmean(returns) / downside * math.sqrt(252))
    assert result['turnover'] == pytest.approx(32 / fmean(equities))
    assert result['fee_ratio'] == pytest.approx(0.005)
    assert result['market_exposure'] == pytest.approx(0.5)
    assert result['sample_size'] == 7
    assert result['explanations'] == {}
    # Equity is already net of costs; commissions must not be subtracted again.
    assert performance_metrics(points, [], 100)['total_return'] == result['total_return']
    assert_finite_or_explained(result)


@pytest.mark.parametrize(('equities', 'drawdown', 'duration'), [
    ([80, 90, 100, 95], 0.2, 2),  # starting capital is an initial peak
    ([110, 100, 90, 80], 1 - 80 / 110, 3),  # unrecovered episode is retained
    ([100, 100, 100], 0, 0),
])
def test_drawdown_counts_underwater_sessions_not_recovery(equities, drawdown, duration):
    result = performance_metrics(curve(equities), [], 100)
    assert result['max_drawdown'] == pytest.approx(drawdown)
    assert result['drawdown_duration'] == duration
    assert_finite_or_explained(result)


def test_daily_aggregation_uses_new_york_not_utc_date_and_last_snapshot():
    points = [
        EquityPoint(START, Decimal('999'), Decimal('999'), Decimal('0')),
        EquityPoint(datetime(2024, 1, 3, 0, 30, tzinfo=timezone.utc),
                    Decimal('110'), Decimal('110'), Decimal('55')),
        EquityPoint(START + timedelta(days=1), Decimal('99'), Decimal('99'), Decimal('99')),
    ]
    result = performance_metrics(iter(points), iter([]), 100)
    assert result['sample_size'] == 2
    assert result['total_return'] == pytest.approx(-0.01)
    assert result['volatility'] == pytest.approx(stdev([0.1, -0.1]) * math.sqrt(252))
    assert result['max_drawdown'] == pytest.approx(0.1)
    assert result['market_exposure'] == pytest.approx(0.75)
    assert result == performance_metrics(points, [], 100)
    assert_finite_or_explained(result)


def test_empty_curve_never_invents_zero_returns():
    result = performance_metrics([], [], 100)
    assert result['sample_size'] == 0
    for key in METRICS - {'sample_size'}:
        assert result[key] is None
        assert 'No equity' in result['explanations'][key]
    assert_finite_or_explained(result)


def test_one_session_includes_first_return_but_cannot_annualize_or_estimate_moments():
    result = performance_metrics(curve([90]), [], 100)
    assert result['total_return'] == pytest.approx(-0.1)
    assert result['max_drawdown'] == pytest.approx(0.1)
    assert result['drawdown_duration'] == 1
    assert result['sample_size'] == 1
    for key in ('annualized_return', 'volatility', 'sharpe', 'sortino'):
        assert result[key] is None
        assert 'At least two' in result['explanations'][key]
    assert_finite_or_explained(result)


def test_flat_cash_and_no_downside_are_not_infinite_risk_adjusted_returns():
    cash = performance_metrics(curve([100] * 3), [], 100)
    for key in ('total_return', 'annualized_return', 'max_drawdown', 'volatility',
                'turnover', 'fee_ratio', 'market_exposure'):
        assert cash[key] == 0
    assert cash['sharpe'] is None
    assert cash['sortino'] is None
    assert 'variance is zero' in cash['explanations']['sharpe']
    positive = performance_metrics(curve([105, 110, 115]), [], 100)
    assert positive['sharpe'] is not None
    assert positive['sortino'] is None
    assert 'No downside' in positive['explanations']['sortino']
    assert_finite_or_explained(cash)
    assert_finite_or_explained(positive)


@pytest.mark.parametrize('capital', [0, -1, float('nan'), float('inf'), Decimal('NaN'), None, 'bad'])
def test_invalid_starting_capital_is_explained(capital):
    result = performance_metrics(curve([100, 101]), [], capital)
    assert result['sample_size'] == 2
    for key in METRICS - {'sample_size'}:
        assert result[key] is None
        assert 'Starting capital' in result['explanations'][key]
    assert_finite_or_explained(result)


@pytest.mark.parametrize('equity', [-1, Decimal('NaN'), Decimal('Infinity')])
def test_invalid_equity_does_not_hide_behind_a_valid_final_point(equity):
    result = performance_metrics(curve([equity, 101]), [], 100)
    assert result['sample_size'] == 2
    assert result['total_return'] is None
    assert 'nonnegative and finite' in result['explanations']['total_return']
    assert_finite_or_explained(result)


@pytest.mark.parametrize('kind', ['duplicate', 'descending', 'naive', 'non_utc', 'not_datetime'])
def test_bad_chronology_is_rejected_not_silently_sorted(kind):
    points = curve([100, 101])
    if kind == 'duplicate':
        points[1] = replace(points[1], at=points[0].at)
    elif kind == 'descending':
        points.reverse()
    elif kind == 'naive':
        points[0] = replace(points[0], at=START.replace(tzinfo=None))
    elif kind == 'non_utc':
        points[0] = replace(points[0], at=START.astimezone(ZoneInfo('America/New_York')))
    else:
        points[0] = replace(points[0], at='2024-01-02')
    result = performance_metrics(points, [], 100)
    assert result['sample_size'] == 0
    assert all(result[key] is None for key in METRICS - {'sample_size'})
    assert_finite_or_explained(result)


@pytest.mark.parametrize('changes', [
    {'quantity': 0}, {'quantity': -1}, {'quantity': True}, {'quantity': 1.5},
    {'price': Decimal('0')}, {'price': Decimal('NaN')}, {'price': Decimal('Infinity')},
    {'commission': Decimal('-1')}, {'commission': Decimal('NaN')}, {'side': 'HOLD'},
])
def test_invalid_fill_does_not_fabricate_turnover_or_fees(changes):
    result = performance_metrics(curve([100, 101]), [fill(**changes)], 100)
    assert result['turnover'] is None
    assert result['fee_ratio'] is None
    assert result['total_return'] == pytest.approx(0.01)
    assert_finite_or_explained(result)


def test_duplicate_fill_ids_make_execution_accounting_unavailable():
    result = performance_metrics(curve([100, 101]), [fill(), fill()], 100)
    assert result['turnover'] is None
    assert result['fee_ratio'] is None
    assert 'Duplicate fill IDs' in result['explanations']['fee_ratio']
    assert_finite_or_explained(result)


@pytest.mark.parametrize('exposure', [-1, Decimal('NaN'), Decimal('Infinity')])
def test_invalid_exposure_is_not_silently_omitted(exposure):
    result = performance_metrics(curve([100, 101], [exposure, 50]), [], 100)
    assert result['market_exposure'] is None
    assert result['total_return'] == pytest.approx(0.01)
    assert_finite_or_explained(result)


def test_terminal_zero_equity_records_total_loss_without_dividing_by_zero():
    result = performance_metrics(curve([100, 0]), [], 100)
    assert result['total_return'] == -1
    assert result['annualized_return'] == -1
    assert result['max_drawdown'] == 1
    assert result['drawdown_duration'] == 1
    assert result['volatility'] == pytest.approx(stdev([0, -1]) * math.sqrt(252))
    assert result['market_exposure'] is None
    assert_finite_or_explained(result)


def test_zero_previous_equity_makes_daily_ratios_undefined():
    result = performance_metrics(curve([0, 10]), [], 100)
    assert result['total_return'] == pytest.approx(-0.9)
    for key in ('volatility', 'sharpe', 'sortino'):
        assert result[key] is None
        assert 'prior equity is zero' in result['explanations'][key]
    assert_finite_or_explained(result)


def test_overflow_is_reported_as_unavailable_and_json_stays_finite():
    result = performance_metrics(curve(['1e308', '1e308']), [], Decimal('1e-308'))
    assert result['total_return'] is None
    assert result['annualized_return'] is None
    assert_finite_or_explained(result)
