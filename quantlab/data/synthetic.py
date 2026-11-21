"""Reproducible software fixtures, deliberately not an investment backtest."""
from __future__ import annotations

import hashlib
import random
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from quantlab.models import CorporateAction, Dataset, MarketBar
from .calendar import CALENDAR_END, session_times
from .integrity import DATA_LIMITATIONS, _valid_symbol, dataset_version

SYNTHETIC_START = date(2022, 1, 3)
_SYNTHETIC_LIMITATION = (
    'SYNTHETIC: invented prices, liquidity, publication times and corporate actions '
    'for deterministic software tests only. Not actual SPY/QQQ/IWM/TLT history; '
    'regimes and correlations are engineered and cannot demonstrate strategy alpha '
    'or validate investment performance. Back-adjustments include future actions.'
)


def _rng(seed, label):
    digest = hashlib.sha256(f'quantlab-synthetic-v1:{seed}:{label}'.encode('ascii')).digest()
    return random.Random(int.from_bytes(digest, 'big'))


def _price(value):
    return value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_EVEN)


def generate_synthetic(seed=7, sessions=504, symbols=('SPY', 'QQQ', 'IWM', 'TLT')) -> Dataset:
    """Invent ``sessions`` XNYS daily bars per symbol starting 2022-01-03.

    Entirely offline. Seeded integer noise and Decimal arithmetic, fixed dates,
    and canonical symbol ordering make values/hash reproducible, independent of
    today's date, input symbol ordering and the caller's Decimal precision.
    Includes quarterly fictional dividends and one fictional 2:1 split when
    enough history is requested. adjusted_close is backwards-adjusted research
    data, never an executable price and never a point-in-time total-return feed.
    """
    if type(seed) is not int:
        raise ValueError('seed must be an integer')
    if type(sessions) is not int or not 1 <= sessions <= 10000:
        raise ValueError('sessions must be a positive integer no greater than 10000')
    if isinstance(symbols, str):
        raise ValueError('symbols must be a sequence, not one string')
    symbols = tuple(symbols)
    if not symbols or any(not _valid_symbol(symbol) for symbol in symbols):
        raise ValueError('At least one canonical uppercase US equity/ETF symbol is required')
    if len(set(symbols)) != len(symbols):
        raise ValueError('Duplicate symbols are not allowed')
    symbols = tuple(sorted(symbols))
    schedule = []
    day = SYNTHETIC_START
    while len(schedule) < sessions:
        if day > CALENDAR_END:
            raise ValueError('Requested history exceeds supported XNYS calendar')
        times = session_times(day)
        if times:
            schedule.append((day, *times))
        day += timedelta(days=1)

    market_rng = _rng(seed, 'market')
    common = [market_rng.randint(-80, 80) for _ in schedule]
    bars = []
    actions = []
    split_symbol = 'SPY' if 'SPY' in symbols else symbols[0]
    with localcontext() as ctx:
        ctx.prec = 38
        ctx.rounding = ROUND_HALF_EVEN
        for symbol in symbols:
            rng = _rng(seed, symbol)
            previous = Decimal(80 + int.from_bytes(hashlib.sha256(symbol.encode('ascii')).digest()[:2], 'big') % 180)
            symbol_bars = []
            symbol_actions = {}
            for index, (day, open_at, close_at) in enumerate(schedule):
                split = Decimal('2') if index == 252 and symbol == split_symbol else Decimal('1')
                dividend = Decimal('0.15') if index > 0 and index % 63 == 0 else Decimal('0')
                if split != 1 or dividend:
                    action = CorporateAction(symbol, day, schedule[index - 1][2] + timedelta(minutes=1), split, dividend)
                    actions.append(action)
                    symbol_actions[index] = action
                theoretical = max(Decimal('1'), previous / split - dividend)
                # Multiple up/down volatility regimes, not a constructed winning
                # strategy. The bond-labelled fixture has opposing common noise.
                regime = (4, -5, 2, -1)[(index // 126) % 4]
                common_move = -common[index] // 2 if symbol == 'TLT' else common[index]
                overnight_bps = rng.randint(-35, 35)
                intraday_bps = common_move + rng.randint(-60, 60) + regime
                opening = _price(theoretical * (1 + Decimal(overnight_bps) / 10000))
                closing = _price(opening * (1 + Decimal(intraday_bps) / 10000))
                high = _price(max(opening, closing) * (1 + Decimal(rng.randint(2, 70)) / 10000))
                low = _price(min(opening, closing) * (1 - Decimal(rng.randint(2, 70)) / 10000))
                symbol_bars.append(MarketBar(
                    symbol, day, open_at, close_at, close_at + timedelta(minutes=1),
                    opening, high, low, closing, closing, rng.randint(800_000, 8_000_000),
                ))
                previous = closing
            # Correct historical split/dividend factors without adjusting any
            # executable OHLC. Factors use pre-ex close and post-split dividend.
            factor = Decimal('1')
            for index in range(len(symbol_bars) - 1, -1, -1):
                bar = symbol_bars[index]
                symbol_bars[index] = replace(bar, adjusted_close=_price(bar.close * factor))
                action = symbol_actions.get(index)
                if action:
                    reference = symbol_bars[index - 1].close / action.split_ratio
                    factor *= (1 - action.cash_dividend / reference) / action.split_ratio
            bars.extend(symbol_bars)
    bars.sort(key=lambda bar: (bar.session, bar.symbol))
    actions.sort(key=lambda action: (action.ex_date, action.symbol))
    dataset = Dataset(
        version='', source=f'SYNTHETIC v1; seed={seed}; start={SYNTHETIC_START.isoformat()}',
        synthetic=True, bars=bars, actions=actions,
        limitations=[_SYNTHETIC_LIMITATION, *DATA_LIMITATIONS],
    )
    dataset.version = dataset_version(dataset)
    return dataset
