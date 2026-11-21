"""Deterministic, long-only research candidates, never executable orders.

Both rules use adjusted_close for research, not raw execution prices. Visibility
checks do NOT make a vendor's retrospectively adjusted history point-in-time.
See STRATEGY_LIMITATIONS before interpreting a historical result as evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from math import isfinite
from zoneinfo import ZoneInfo

from quantlab.models import MarketBar, Signal, require_utc

__all__ = ['generate_signals', 'STRATEGY_LIMITATIONS']

STRATEGY_LIMITATIONS = (
    'available_at and close_at gate row visibility only. adjusted_close must be '
    'point-in-time: future corporate-action adjustments or historical revisions '
    'can still introduce look-ahead bias when old values are overwritten.',
    'The supplied universe is not reconstructed historically. Missing delistings '
    'and historical constituents can introduce survivorship and selection bias.',
    'Windows use distinct observed exchange-session dates, not a reconstructed '
    'exchange calendar. A session missing from every symbol cannot be detected; '
    'partially missing windows stay cash and prices are never forward-filled.',
    'Synthetic data tests software behavior, not investment performance or edge.',
)

_EXCHANGE_ZONE = ZoneInfo('America/New_York')


def _utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f'{name} must be a timezone-aware UTC datetime')
    try:
        require_utc(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be a timezone-aware UTC datetime') from exc


def _parameters(strategy: str, lookback: int, skip: int, top_n: int) -> None:
    if not isinstance(strategy, str) or strategy not in ('trend', 'momentum'):
        raise ValueError("strategy must be 'trend' or 'momentum'")
    for name, value, minimum in (
        ('lookback', lookback, 1), ('skip', skip, 0), ('top_n', top_n, 1)
    ):
        # bool is an int subclass, but is not a meaningful research parameter.
        if type(value) is not int or value < minimum:
            raise ValueError(f'{name} must be an integer >= {minimum}')
    if strategy == 'momentum' and skip >= lookback:
        raise ValueError('momentum skip must be smaller than lookback')


def _validate_visible(bar: MarketBar) -> None:
    """Reject corrupted known observations instead of silently changing history."""
    if not isinstance(bar.symbol, str) or not bar.symbol or bar.symbol.strip() != bar.symbol:
        raise ValueError('symbol must be a non-empty string without outer whitespace')
    if type(bar.session) is not date:
        raise ValueError(f'{bar.symbol}: session must be an exchange trading date')
    _utc(bar.open_at, 'open_at')
    if bar.open_at >= bar.close_at:
        raise ValueError(f'{bar.symbol}: open_at must precede close_at')
    if any(at.astimezone(_EXCHANGE_ZONE).date() != bar.session
           for at in (bar.open_at, bar.close_at)):
        raise ValueError(f'{bar.symbol}: session must match the New York trading date')
    for name in ('open', 'high', 'low', 'close', 'adjusted_close'):
        value = getattr(bar, name)
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f'{bar.symbol}: {name} must be a positive finite Decimal')
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        raise ValueError(f'{bar.symbol}: inconsistent OHLC bounds')
    if type(bar.volume) is not int or bar.volume < 0:
        raise ValueError(f'{bar.symbol}: volume must be a non-negative integer')


def _return(start: Decimal, end: Decimal, symbol: str) -> tuple[Decimal, float]:
    # Fix decimal arithmetic context so caller precision/rounding cannot change
    # rankings. Keep Decimal through ranking; float is only the model's score.
    try:
        with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
            value = end / start - Decimal('1')
            score = float(value)
    except (DecimalException, OverflowError, ValueError) as exc:
        raise ValueError(f'{symbol}: return cannot be represented as a finite score') from exc
    if not isfinite(score):
        raise ValueError(f'{symbol}: return cannot be represented as a finite score')
    return value, score


def generate_signals(
    bars: Iterable[MarketBar],
    as_of: datetime,
    strategy: str = 'trend',
    lookback: int = 63,
    skip: int = 5,
    top_n: int = 2,
) -> list[Signal]:
    """Return one explained candidate per *visible* symbol, sorted lexically.

    Only bars with available_at <= as_of AND close_at <= as_of participate,
    including in symbol discovery, validation of prices, and window selection.
    Timestamp metadata needed for that gate must itself be valid UTC.

    Let t be the latest session in the union of visible bars. The window is the
    last lookback + 1 distinct observed sessions, shared across symbols to avoid
    comparing unequal horizons. Every session in this window must be present for
    a symbol, even skipped sessions: missing/late observations mean cash, with
    score 0. No calendar-day interpolation or forward-filling is performed.

    trend: adjusted_close[t] / adjusted_close[t-lookback] - 1; every strictly
    positive return is eligible. skip and top_n are validated but do not apply.
    momentum: adjusted_close[t-skip] / adjusted_close[t-lookback] - 1;
    0 <= skip < lookback. Rank positive returns descending, break exact ties by
    symbol ascending, and select at most top_n (top_n may exceed universe size).
    Negative/flat returns never create short candidates. Eligibility is not an
    order or allocation, and an ineligible symbol leaves its allocation in cash.

    Invalid parameters and visible data raise ValueError. Identical duplicate
    bars are idempotent; conflicting visible bars for one symbol/session are
    rejected rather than implicitly choosing a historical revision. Iterables
    and arbitrary input order are supported; inputs are not mutated.

    STRATEGY_LIMITATIONS applies: in particular, the availability gate cannot
    undo future-informed adjustments already embedded in historical prices.
    """
    _parameters(strategy, lookback, skip, top_n)
    _utc(as_of, 'as_of')
    try:
        observations = iter(bars)
    except TypeError as exc:
        raise ValueError('bars must be an iterable of MarketBar') from exc

    history: dict[str, dict[date, MarketBar]] = {}
    sessions: set[date] = set()
    for bar in observations:
        if not isinstance(bar, MarketBar):
            raise ValueError('bars must contain only MarketBar observations')
        _utc(bar.available_at, 'available_at')
        _utc(bar.close_at, 'close_at')
        if bar.available_at > as_of or bar.close_at > as_of:
            continue
        _validate_visible(bar)
        symbol_history = history.setdefault(bar.symbol, {})
        previous = symbol_history.get(bar.session)
        if previous is not None and previous != bar:
            raise ValueError(f'{bar.symbol}: conflicting bars for session {bar.session}')
        symbol_history[bar.session] = bar
        sessions.add(bar.session)

    if not history:
        return []

    required = lookback + 1
    window = sorted(sessions)[-required:]
    results: dict[str, Signal] = {}
    scores: dict[str, tuple[Decimal, float]] = {}
    for symbol in sorted(history):
        known = history[symbol]
        present = sum(day in known for day in window)
        if len(window) < required or present < required:
            results[symbol] = Signal(
                symbol=symbol, as_of=as_of, score=0.0, eligible=False,
                reason=(f'{strategy}: warmup/incomplete_window; need {required} closed, '
                        f'available observations in the common window, have {present}; '
                        'cash (no price filling)'),
            )
            continue
        end_index = -1 if strategy == 'trend' else -1 - skip
        scores[symbol] = _return(
            known[window[0]].adjusted_close,
            known[window[end_index]].adjusted_close,
            symbol,
        )

    # Comparing Decimal scores avoids float rounding changing the selection.
    # Two stable sorts give descending scores with ascending lexical ties, and
    # avoid unary Decimal negation (which would use the caller's context).
    ranked = sorted(symbol for symbol, (value, _) in scores.items() if value > 0)
    ranked.sort(key=lambda symbol: scores[symbol][0], reverse=True)
    ranks = {symbol: rank for rank, symbol in enumerate(ranked, start=1)}

    for symbol, (value, score) in scores.items():
        positive = value > 0
        if strategy == 'trend':
            eligible = positive
            detail = 'positive trend' if positive else 'non-positive trend'
            parameters = f'lookback={lookback}'
        else:
            rank = ranks.get(symbol)
            eligible = positive and rank <= top_n
            detail = (f'positive momentum rank={rank}/{len(ranked)}'
                      if positive else 'non-positive momentum (not ranked)')
            if positive and not eligible:
                detail += '; outside top_n'
            parameters = f'lookback={lookback}, skip={skip}, top_n={top_n}'
        outcome = 'long-only candidate, not an order' if eligible else 'cash'
        results[symbol] = Signal(
            symbol=symbol, as_of=as_of, score=score, eligible=eligible,
            reason=f'{strategy}: {parameters}; return={value}; {detail}; {outcome}',
        )
    return [results[symbol] for symbol in sorted(results)]
