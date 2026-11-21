from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from quantlab.backtest import Ledger, run_backtest
from quantlab.models import BacktestConfig, CorporateAction, Dataset, Fill, MarketBar, TargetWeight, jsonable

UTC = timezone.utc


def dataset(volumes=(100000, 100000, 100000, 100000), prices=(100, 101, 102, 101)):
    bars = []
    for index, (volume, price) in enumerate(zip(volumes, prices)):
        day = date(2024, 1, 2 + index)
        opened = datetime(2024, 1, day.day, 14, 30, tzinfo=UTC)
        closed = opened + timedelta(hours=6, minutes=30)
        p = D(str(price))
        bars.append(MarketBar('SPY', day, opened, closed, closed + timedelta(minutes=1), p, p+1, p-1, p, p, volume))
    return Dataset('fixture-v1', 'synthetic test fixture', True, bars)


def targets(bars, at, account):
    return [TargetWeight('SPY', D('.30'), 'test')]


def test_next_day_raw_execution_and_costs():
    data = dataset()
    result = run_backtest(data, targets)
    first = result.fills[0]
    assert first.at == data.bars[1].open_at
    assert first.price == D('101.0606')
    assert first.commission >= D('1')
    assert first.at > result.orders[0].intent.created_at
    assert result.final_account.cash >= 0
    assert all(p.quantity >= 0 for p in result.final_account.positions.values())
    assert result.final_account.equity == result.final_account.cash + sum(p.quantity*p.market_price for p in result.final_account.positions.values())


def test_reproducible_and_future_volume_does_not_help_open():
    data = dataset(volumes=(100, 10000000, 10000000, 10000000))
    result = run_backtest(data, targets)
    assert result.fills[0].quantity == 1
    assert result.orders[0].status == 'EXPIRED'
    assert result.orders[0].filled_quantity == 1
    assert jsonable(result) == jsonable(run_backtest(data, targets))


def test_no_previous_volume_means_no_fill():
    result = run_backtest(dataset(volumes=(0, 0, 0, 0)), targets)
    assert result.fills == []
    assert all(order.status == 'EXPIRED' for order in result.orders)


def test_gtc_partials_never_overfill_and_minimum_fee_once():
    result = run_backtest(dataset(volumes=(100, 100, 100, 100)), targets, BacktestConfig(order_tif='GTC'))
    assert len(result.orders) == 1
    assert result.orders[0].status == 'PARTIALLY_FILLED'
    assert result.orders[0].filled_quantity == 3
    assert sum(fill.commission for fill in result.fills) == D('1')


def test_independent_risk_rejects_oversized_weight_and_gap():
    result = run_backtest(dataset(), lambda *args: [TargetWeight('SPY', D('.8'), 'oversized')])
    assert not result.fills
    assert all('position_weight_limit' in order.reason for order in result.orders)
    gap = dataset(prices=(100, 110, 111, 112))
    result = run_backtest(gap, targets)
    assert result.orders[0].status == 'REJECTED'
    assert 'price_deviation' in result.orders[0].reason


def test_late_rows_not_visible_to_callback():
    data = dataset()
    first = data.bars[0]
    data.bars[0] = replace(first, available_at=data.bars[-1].available_at)
    seen = []
    run_backtest(data, lambda bars, at, account: seen.append((bars, at)) or [])
    assert all(bar.available_at <= at for bars, at in seen for bar in bars)
    assert not seen[0][0]


def test_adjusted_close_not_used_as_execution_price():
    data = dataset()
    data.bars = [replace(bar, adjusted_close=bar.close / 10) for bar in data.bars]
    result = run_backtest(data, targets)
    assert result.fills[0].price > D('100')


def test_sell_precedes_buy_and_no_financing():
    data = dataset()
    data.bars += [replace(b, symbol='QQQ') for b in list(data.bars)]
    def rotation(bars, at, account):
        return [TargetWeight('SPY' if at.day < 3 else 'QQQ', D('.3'), 'rotation')]
    result = run_backtest(data, rotation)
    on_day = [fill.side for fill in result.fills if fill.at.day == 4]
    assert on_day[0] == 'SELL'
    assert on_day[-1] == 'BUY'
    assert min(p.cash for p in result.equity_curve) >= 0


def test_ledger_duplicate_fill_and_rejected_mutation():
    ledger = Ledger(D('1000'))
    fill = Fill('f1', 'o1', 'SPY', 'BUY', 2, D('100'), D('1'), dataset().bars[0].open_at)
    assert ledger.apply_fill(fill)
    assert ledger.apply_fill(fill) is False
    assert ledger.cash == D('799')
    with pytest.raises(ValueError):
        ledger.apply_fill(replace(fill, quantity=3))
    with pytest.raises(ValueError):
        ledger.apply_fill(replace(fill, fill_id='f2', side='SELL', quantity=3))
    with pytest.raises(ValueError):
        ledger.apply_fill(replace(fill, fill_id='f3', quantity=50))
    assert ledger.cash == D('799')


def test_splits_and_dividends_once():
    ledger = Ledger(D('1000'))
    bars = dataset().bars
    ledger.apply_fill(Fill('f1', 'o1', 'SPY', 'BUY', 3, D('100'), D('0'), bars[0].open_at))
    action = CorporateAction('SPY', bars[1].session, bars[0].open_at, D('2'), D('1'))
    assert ledger.apply_corporate_action(action, bars[1].open_at, D('50'))
    assert not ledger.apply_corporate_action(action, bars[1].open_at, D('50'))
    assert ledger.positions['SPY'].quantity == 6
    assert ledger.cash == D('706')
    assert ledger.snapshot().equity == D('1006')


def test_backtest_corporate_actions_preserve_value():
    data = dataset(prices=(100, 100, 50, 50))
    data.actions = [CorporateAction('SPY', data.bars[2].session, data.bars[0].open_at, D('2'))]
    entered = False
    def buy_once(*args):
        nonlocal entered
        if entered:
            return None
        entered = True
        return targets(*args)
    result = run_backtest(data, buy_once)
    assert len(result.fills) == 1
    assert result.final_account.positions['SPY'].quantity == 600
    assert result.equity_curve[-1].equity == result.equity_curve[1].equity


@pytest.mark.parametrize('kwargs', [{'max_participation': D('0')}, {'slippage_bps': D('10000')}, {'initial_cash': D('-1')}, {'rebalance_every': 0}])
def test_bad_config(kwargs):
    with pytest.raises(ValueError):
        run_backtest(dataset(), targets, BacktestConfig(**kwargs))
