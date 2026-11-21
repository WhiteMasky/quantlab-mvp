"""Isolated protocol doubles, NOT market history, execution models or evidence.

Dates are arbitrary weekday labels, not an XNYS calendar implementation. The
scripted equity changes below only exercise validation branches. Tests that
change a synthetic flag test flag propagation, never establish real returns.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantlab.models import AccountSnapshot, BacktestResult, Dataset, EquityPoint, Fill, MarketBar

UTC = timezone.utc
NY = ZoneInfo('America/New_York')


def make_dataset(sessions=300, symbol='SPY'):
    day = date(2023, 1, 3)
    bars = []
    while len(bars) < sessions:
        if day.weekday() < 5:
            opening = datetime.combine(day, time(9, 30), NY).astimezone(UTC)
            closing = datetime.combine(day, time(16), NY).astimezone(UTC)
            bars.append(MarketBar(
                symbol, day, opening, closing, closing + timedelta(minutes=1),
                Decimal('100'), Decimal('101'), Decimal('99'), Decimal('100'),
                Decimal('100'), 10000,
            ))
        day += timedelta(days=1)
    return Dataset('unit-test-fixture-v1', 'UNIT_TEST_ONLY: invented flat bars', True, bars,
                   limitations=['UNIT_TEST_ONLY: not an exchange calendar or market history'])


class ScriptedEvaluator:
    """Return explicit BacktestResults; do not invoke engine, strategy or broker.

    Prices do not generate this equity path. The script sets a warmup endpoint,
    then applies specified test returns and a single explicit commission. This
    deliberately cannot be used as a real backtest or profitability claim.
    """

    def __init__(self, rates=None, failures=(), transform=None, result_synthetic=None,
                 warmup_start='100', warmup_end='1000'):
        self.rates = rates or {}
        self.failures = set(failures)
        self.transform = transform
        self.result_synthetic = result_synthetic
        self.warmup_start = Decimal(warmup_start)
        self.warmup_end = Decimal(warmup_end)
        self.calls = []
        self.results = []

    def __call__(self, subset, parameters, cost_multiplier):
        key = (subset.role, parameters['lookback'], cost_multiplier)
        self.calls.append((deepcopy(subset), deepcopy(parameters), cost_multiplier))
        if subset.role in self.failures or key in self.failures:
            self.results.append(None)
            raise RuntimeError(f'scripted failure: {key}')
        rate = Decimal(str(self.rates.get(key, self.rates.get(subset.role, '0.002'))))
        bars_by_day = {}
        for bar in subset.bars:
            bars_by_day.setdefault(bar.session, []).append(bar)
        curve = []
        for index, day in enumerate(subset.warmup_sessions):
            # Arbitrarily large warmup profit/loss must not affect scored returns.
            equity = self.warmup_end if index == len(subset.warmup_sessions) - 1 else self.warmup_start
            curve.append(EquityPoint(max(bar.close_at for bar in bars_by_day[day]),
                                     equity, equity, Decimal('0')))
        commission = Decimal(str(cost_multiplier))
        equity = self.warmup_end
        for index, day in enumerate(subset.evaluation_sessions):
            equity *= 1 + rate
            if index == 0:
                equity -= commission
            curve.append(EquityPoint(max(bar.close_at for bar in bars_by_day[day]),
                                     equity, equity, Decimal('0')))
        fills = [
            Fill('warmup-only', 'warmup-order', 'SPY', 'BUY', 50, Decimal('10'),
                 Decimal('999'), curve[len(subset.warmup_sessions) - 1].at),
            Fill('scored-only', 'scored-order', 'SPY', 'SELL', 2, Decimal('10'),
                 commission, subset.evaluation_start_at),
        ]
        result = BacktestResult(
            equity_curve=curve, orders=[], fills=fills,
            final_account=AccountSnapshot(equity, equity, {}, curve[-1].at),
            metrics={'total_return': 123456, 'sample_size': 987654},
            warnings=['UNIT_TEST_ONLY: scripted equity, not execution or investment evidence'],
            synthetic=subset.synthetic if self.result_synthetic is None else self.result_synthetic,
        )
        if self.transform is not None:
            result = self.transform(result, subset, parameters, cost_multiplier)
        self.results.append(deepcopy(result))
        return result


@pytest.fixture
def dataset_factory():
    return make_dataset


@pytest.fixture
def dataset():
    return make_dataset()


@pytest.fixture
def grid():
    return [{'lookback': value, 'skip': 0, 'vol_lookback': 2} for value in (2, 3, 4)]


@pytest.fixture
def evaluator_factory():
    return ScriptedEvaluator
