"""Daily, net-of-execution-cost research metrics; undefined values stay None."""
from __future__ import annotations

import math
from datetime import datetime
from statistics import fmean, stdev
from zoneinfo import ZoneInfo

from quantlab.models import require_utc

NEW_YORK = ZoneInfo('America/New_York')
METRIC_NAMES = (
    'total_return', 'annualized_return', 'max_drawdown', 'drawdown_duration',
    'volatility', 'sharpe', 'sortino', 'turnover', 'fee_ratio',
    'market_exposure', 'sample_size',
)


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _daily_points(curve):
    """Keep the final observation per New York date, never reorder bad input."""
    daily = {}
    previous = None
    for point in curve:
        if not isinstance(point.at, datetime):
            raise ValueError('Equity timestamps must be UTC datetimes')
        require_utc(point.at)
        if previous is not None and point.at <= previous:
            raise ValueError('Equity timestamps must be strictly increasing')
        previous = point.at
        daily[point.at.astimezone(NEW_YORK).date()] = point
    return list(daily.values())


def _returns(points, initial_cash):
    previous = _number(initial_cash)
    if previous is None or previous <= 0:
        return None
    returns = []
    for point in points:
        equity = _number(point.equity)
        if equity is None or equity < 0 or previous <= 0:
            return None
        value = equity / previous - 1
        if not math.isfinite(value):
            return None
        returns.append(value)
        previous = equity
    return returns


def performance_metrics(curve, fills, initial_cash) -> dict:
    """Score daily EOD observations against capital immediately BEFORE the period.

    The first EOD return is included. Intraday snapshots collapse to the last
    per exchange date. Annualization assumes consecutive daily trading-session
    observations and 252 sessions/year, with zero risk-free/target return.
    Drawdown is a positive loss fraction; duration counts underwater sessions
    (including an unrecovered episode). Turnover is BUY+SELL notional / average
    EOD equity, not halved or annualized. Fee ratio is commissions / starting
    capital, NOT total modeled spread/slippage, which are already in equity.
    EquityPoint.gross_exposure is USD gross position notional; market_exposure
    is its mean daily ratio to equity. No cash flows are inferred or adjusted.
    """
    result = {key: None for key in METRIC_NAMES}
    result['sample_size'] = 0
    reasons = result['explanations'] = {}
    result['definitions'] = {
        'frequency': 'last snapshot per New York session date; consecutive daily sessions assumed',
        'annualization': '252 trading sessions/year; zero risk-free and downside target',
        'drawdown_duration': 'maximum consecutive observed underwater sessions; recovery day excluded',
        'turnover': 'sum absolute executed notional / mean EOD equity; not annualized or halved',
        'fee_ratio': 'explicit commissions / capital immediately before scored period',
        'market_exposure': 'mean gross USD position notional / EOD equity',
        'sample_size': 'scored daily EOD observations, including initial-capital-to-first return',
    }

    def unavailable(keys, reason):
        for key in keys:
            reasons[key] = reason

    try:
        points = _daily_points(list(curve))
    except (AttributeError, TypeError, ValueError) as exc:
        unavailable(METRIC_NAMES[:-1], str(exc))
        reasons['sample_size'] = 'No valid chronological daily observations'
        return result
    result['sample_size'] = len(points)
    if not points:
        unavailable(METRIC_NAMES[:-1], 'No equity observations')
        return result
    initial = _number(initial_cash)
    equities = [_number(point.equity) for point in points]
    if initial is None or initial <= 0:
        unavailable(METRIC_NAMES[:-1], 'Starting capital must be positive and finite')
        return result
    if any(value is None or value < 0 for value in equities):
        unavailable(METRIC_NAMES[:-1], 'Equity must be nonnegative and finite')
        return result

    result['total_return'] = equities[-1] / initial - 1
    peak, duration, longest, max_dd = initial, 0, 0, 0.0
    for equity in equities:
        peak = max(peak, equity)
        dd = 1 - equity / peak
        duration = duration + 1 if equity < peak else 0
        longest, max_dd = max(longest, duration), max(max_dd, dd)
    result['max_drawdown'], result['drawdown_duration'] = max_dd, longest
    returns = _returns(points, initial)
    if len(points) < 2:
        unavailable(('annualized_return', 'volatility', 'sharpe', 'sortino'),
                    'At least two daily return observations are required')
    else:
        try:
            result['annualized_return'] = (equities[-1] / initial) ** (252 / len(points)) - 1
        except OverflowError:
            reasons['annualized_return'] = 'Annualization overflow'
        if returns is None:
            unavailable(('volatility', 'sharpe', 'sortino'),
                        'A prior equity is zero or a daily return is not finite')
        else:
            try:
                sigma = stdev(returns)
                mean = fmean(returns)
                result['volatility'] = sigma * math.sqrt(252)
                if sigma > 0:
                    result['sharpe'] = mean / sigma * math.sqrt(252)
                else:
                    reasons['sharpe'] = 'Daily return variance is zero'
                downside = math.sqrt(fmean(min(value, 0) ** 2 for value in returns))
                if downside > 0:
                    result['sortino'] = mean / downside * math.sqrt(252)
                else:
                    reasons['sortino'] = 'No downside deviations from the zero target'
            except (OverflowError, ValueError):
                unavailable(('volatility', 'sharpe', 'sortino'), 'Return moment overflow')

    try:
        notionals, commissions, seen = [], [], set()
        for fill in fills:
            price, fee = _number(fill.price), _number(fill.commission)
            if (type(fill.quantity) is not int or fill.quantity <= 0 or price is None
                    or price <= 0 or fee is None or fee < 0 or fill.side not in ('BUY', 'SELL')):
                raise ValueError('Invalid execution quantity, price, commission or side')
            if fill.fill_id in seen:
                raise ValueError('Duplicate fill IDs: ambiguous execution accounting')
            seen.add(fill.fill_id)
            notionals.append(fill.quantity * price)
            commissions.append(fee)
        average = fmean(equities)
        if average > 0:
            result['turnover'] = math.fsum(notionals) / average
        else:
            reasons['turnover'] = 'Mean scored equity is zero'
        result['fee_ratio'] = math.fsum(commissions) / initial
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        unavailable(('turnover', 'fee_ratio'), str(exc))

    exposure = [_number(point.gross_exposure) for point in points]
    if any(value is None or value < 0 for value in exposure) or any(eq <= 0 for eq in equities):
        reasons['market_exposure'] = 'Requires nonnegative USD gross exposure and positive daily equity'
    else:
        try:
            result['market_exposure'] = fmean(value / eq for value, eq in zip(exposure, equities))
        except (OverflowError, ValueError):
            reasons['market_exposure'] = 'Exposure ratio overflow'
    for key in METRIC_NAMES:
        value = result[key]
        if value is not None and not math.isfinite(value):
            result[key] = None
            reasons[key] = 'Calculation is not finite'
    return result
