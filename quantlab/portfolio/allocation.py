"""Deterministic, long-only allocation; this is a heuristic, not an optimizer."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, localcontext

from quantlab.models import MarketBar, Signal, TargetWeight, require_utc

_ZERO = Decimal('0')
_ONE = Decimal('1')
_WEIGHT_STEP = Decimal('0.000000000001')


def _decimal(value, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a finite number')
    try:
        result = Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f'{name} must be a finite number') from exc
    if not result.is_finite():
        raise ValueError(f'{name} must be finite')
    return result


def _unit(value, name: str) -> Decimal:
    result = _decimal(value, name)
    if not _ZERO <= result <= _ONE:
        raise ValueError(f'{name} must be in [0, 1]')
    return result


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError('symbol must be a nonempty string without outer whitespace')
    return value


def _down(value: Decimal) -> Decimal:
    return value.quantize(_WEIGHT_STEP, rounding=ROUND_DOWN)


def _known_signals(signals, as_of: datetime) -> dict[str, Signal]:
    latest = {}
    for signal in signals:
        require_utc(signal.as_of)
        if signal.as_of > as_of:
            continue
        symbol = _symbol(signal.symbol)
        if type(signal.eligible) is not bool:
            raise ValueError('Signal.eligible must be boolean')
        score = _decimal(signal.score, 'signal score')
        # A later withdrawal supersedes an earlier candidate. At the same time,
        # an ineligible observation wins; otherwise the higher score wins.
        rank = (signal.as_of, not signal.eligible, score)
        if symbol not in latest or rank > latest[symbol][0]:
            latest[symbol] = (rank, signal)
    return {symbol: item[1] for symbol, item in latest.items() if item[1].eligible}


def _histories(bars, symbols: set[str], as_of: datetime, lookback: int):
    grouped: dict[str, dict] = {symbol: {} for symbol in symbols}
    for bar in bars:
        if bar.symbol not in grouped:
            continue
        # Filter BEFORE revisions, rolling windows, prices, or return estimates.
        if bar.available_at > as_of or bar.close_at > as_of or bar.open_at > as_of:
            continue
        by_session = grouped[bar.symbol]
        previous = by_session.get(bar.session)
        if previous is None or bar.available_at > previous.available_at:
            by_session[bar.session] = bar
        elif bar.available_at == previous.available_at and bar != previous:
            raise ValueError(f'Ambiguous known bar for {bar.symbol} on {bar.session}')

    histories = {}
    for symbol, by_session in grouped.items():
        window = [by_session[day] for day in sorted(by_session)][-(lookback + 1):]
        if len(window) < lookback + 1:
            continue
        try:
            prices = [_decimal(bar.adjusted_close, 'adjusted_close') for bar in window]
        except ValueError:
            continue
        if any(price <= 0 for price in prices):
            continue
        returns = {
            (window[i - 1].session, window[i].session): prices[i] / prices[i - 1] - _ONE
            for i in range(1, len(window))
        }
        values = list(returns.values())
        mean = sum(values, _ZERO) / Decimal(lookback)
        variance = sum(((value - mean) ** 2 for value in values), _ZERO) / Decimal(lookback - 1)
        # Constant/invalid histories do not imply a risk-free unlimited allocation.
        if variance <= 0:
            continue
        histories[symbol] = (returns, variance.sqrt())
    return histories


def _compatible(left: str, right: str, histories, lookback: int, limit: Decimal) -> bool:
    if left == right:
        return True
    if left not in histories or right not in histories:
        return False
    left_returns, right_returns = histories[left][0], histories[right][0]
    intervals = sorted(left_returns.keys() & right_returns.keys())
    # Match BOTH endpoints, never align a two-day return with a one-day return.
    if len(intervals) < lookback:
        return False
    xs = [left_returns[key] for key in intervals]
    ys = [right_returns[key] for key in intervals]
    count = Decimal(len(intervals))
    mx, my = sum(xs, _ZERO) / count, sum(ys, _ZERO) / count
    xx = sum(((x - mx) ** 2 for x in xs), _ZERO)
    yy = sum(((y - my) ** 2 for y in ys), _ZERO)
    if xx <= 0 or yy <= 0:
        return False
    covariance = sum(((x - mx) * (y - my) for x, y in zip(xs, ys)), _ZERO)
    correlation = max(-_ONE, min(_ONE, covariance / (xx * yy).sqrt()))
    # Only positive co-movement above the threshold is excluded; anticorrelation
    # is diversification. This is not an absolute-correlation constraint.
    return correlation <= limit


def _inverse_vol_weights(selected: list[str], histories, cap: Decimal, budget: Decimal):
    weights = {}
    remaining = list(selected)
    while remaining and budget > 0 and cap > 0:
        inverse = {symbol: _ONE / histories[symbol][1] for symbol in remaining}
        total = sum(inverse.values(), _ZERO)
        capped = [symbol for symbol in remaining if budget * inverse[symbol] / total >= cap]
        if not capped:
            weights.update({symbol: _down(budget * inverse[symbol] / total) for symbol in remaining})
            break
        for symbol in capped:
            weights[symbol] = _down(cap)
            budget -= weights[symbol]
            remaining.remove(symbol)
    return weights


def _moves(deficits: dict[str, Decimal], budget: Decimal) -> dict[str, Decimal]:
    """Pro-rata moves rounded DOWN, so a tight turnover budget is never exceeded."""
    total = sum(deficits.values(), _ZERO)
    if total <= budget:
        return deficits.copy()
    if total == 0 or budget <= 0:
        return {symbol: _ZERO for symbol in deficits}
    return {symbol: min(value, _down(budget * value / total)) for symbol, value in deficits.items()}


def allocate(
    signals: list[Signal],
    bars: list[MarketBar],
    as_of: datetime,
    max_weight=Decimal('0.35'),
    cash_buffer=Decimal('0.10'),
    vol_lookback=20,
    max_correlation=0.90,
    current_weights=None,
    turnover_limit=Decimal('0.30'),
) -> list[TargetWeight]:
    """Allocate using known adjusted-price returns and capped inverse volatility.

    Candidates are greedily admitted by descending signal score, then symbol,
    only when every admitted pair has <= max_correlation over vol_lookback
    identical return intervals. Missing/zero-volatility history stays cash;
    insufficient pair overlap rejects the later candidate. No optimality claim.
    Historical universe and point-in-time adjusted-price quality remain the
    caller/data provider's responsibility, not guarantees of this estimator.

    Turnover is SUM(abs(new_weight - old_weight)), excluding cash, NOT half
    that sum. None means no known starting portfolio (no turnover constraint);
    {} explicitly means an all-cash portfolio and DOES constrain initial buys.
    Existing holdings are first repaired for cap/exposure/pair constraints;
    if those heuristic safety reductions exceed the limit, raise ValueError
    rather than silently violate a constraint. Thereafter discretionary sells
    precede increases. Non-selected residual holdings may remain because of
    turnover, but block incompatible additions until fully exited. An isolated
    holding without usable history may only be retained/reduced, never added.

    All weight arithmetic is Decimal; sub-1e-12 discretionary moves stay cash.
    Inputs are not mutated and list/generator order does not affect the result.
    """
    require_utc(as_of)
    cap = _unit(max_weight, 'max_weight')
    buffer = _unit(cash_buffer, 'cash_buffer')
    turnover = _unit(turnover_limit, 'turnover_limit')
    correlation = _decimal(max_correlation, 'max_correlation')
    if not -_ONE <= correlation <= _ONE:
        raise ValueError('max_correlation must be in [-1, 1]')
    if type(vol_lookback) is not int or vol_lookback < 2:
        raise ValueError('vol_lookback must be an integer >= 2')
    if current_weights is not None and not isinstance(current_weights, Mapping):
        raise ValueError('current_weights must be a symbol-to-weight mapping')
    current = {
        _symbol(symbol): _unit(weight, 'current weight')
        for symbol, weight in (current_weights or {}).items()
    }
    with localcontext() as context:
        context.prec = 50
        if sum(current.values(), _ZERO) > _ONE:
            raise ValueError('current_weights cannot be leveraged')
        known = _known_signals(signals, as_of)
        histories = _histories(bars, set(known) | set(current), as_of, vol_lookback)
        ranked = sorted(known, key=lambda symbol: (-_decimal(known[symbol].score, 'score'), symbol))
        selected = []
        for symbol in ranked:
            if symbol in histories and all(
                _compatible(symbol, other, histories, vol_lookback, correlation) for other in selected
            ):
                selected.append(symbol)
        desired = _inverse_vol_weights(selected, histories, cap, _ONE - buffer)
        weights = desired
        if current_weights is not None:
            weights = {symbol: min(weight, cap) for symbol, weight in current.items()}
            # Hard constraints are repaired before spending on discretionary trades.
            safety_order = sorted(current, key=lambda symbol: (
                -_decimal(known[symbol].score, 'score') if symbol in known else Decimal('Infinity'),
                symbol,
            ))
            retained = []
            for symbol in safety_order:
                if weights[symbol] <= 0:
                    continue
                if all(_compatible(symbol, other, histories, vol_lookback, correlation) for other in retained):
                    retained.append(symbol)
                else:
                    weights[symbol] = _ZERO
            exposure = sum(weights.values(), _ZERO)
            if exposure > _ONE - buffer:
                weights = {symbol: _down(weight * (_ONE - buffer) / exposure) for symbol, weight in weights.items()}
            spent = sum((current[symbol] - weights[symbol] for symbol in current), _ZERO)
            if spent > turnover:
                raise ValueError('turnover_limit cannot fund heuristic cap/cash/correlation safety reductions')
            remaining = turnover - spent
            reductions = {
                symbol: weight - desired.get(symbol, _ZERO)
                for symbol, weight in weights.items() if weight > desired.get(symbol, _ZERO)
            }
            moves = _moves(reductions, remaining)
            for symbol, value in moves.items():
                weights[symbol] -= value
            remaining -= sum(moves.values(), _ZERO)
            residual = [symbol for symbol, weight in weights.items() if weight > 0]
            increases = {
                symbol: weight - weights.get(symbol, _ZERO)
                for symbol, weight in desired.items()
                if weight > weights.get(symbol, _ZERO) and all(
                    _compatible(symbol, other, histories, vol_lookback, correlation) for other in residual
                )
            }
            room = max(_ZERO, _ONE - buffer - sum(weights.values(), _ZERO))
            for symbol, value in _moves(increases, min(remaining, room)).items():
                weights[symbol] = weights.get(symbol, _ZERO) + value

        return [
            TargetWeight(
                symbol=symbol,
                weight=weight,
                reason=(
                    'capped inverse volatility; greedy pairwise correlation; gross-L1 turnover budget'
                    if symbol in desired else
                    'turnover-limited residual holding; not selected; no new exposure'
                ),
            )
            for symbol, weight in sorted(weights.items()) if weight > 0
        ]
