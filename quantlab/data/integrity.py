"""Content addressing and non-destructive, daily-bar quality diagnostics."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from quantlab.models import Dataset, require_utc
from .calendar import CALENDAR_END, CALENDAR_LIMITATION, CALENDAR_START, session_times

DATA_LIMITATIONS = (
    'Raw OHLC are executable-price inputs; adjusted_close is research-only, '
    'not guaranteed point-in-time and must never be used for execution.',
    'Historical universe membership, delistings and survivorship are not supplied; '
    'missing observations cannot distinguish listing changes, halts or vendor gaps.',
    'Corporate-action coverage is unverified unless independently reconciled. '
    'Only splits and USD cash dividends are modeled; no mergers, spinoffs, '
    'rights, taxes, payment dates or fractional cash-in-lieu valuations.',
    'available_at and known_at are provider assertions, not independently verified '
    'publication timestamps. Data are daily USD bars, not live tradable quotes.',
    CALENDAR_LIMITATION,
)
_SYMBOL = re.compile(r'[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*\Z')
_PRICE_FIELDS = ('open', 'high', 'low', 'close', 'adjusted_close')


class DataIntegrityError(ValueError):
    """Import/cache failure with diagnostics and any parsed dataset retained."""

    def __init__(self, message: str, issues=None, dataset: Dataset | None = None):
        super().__init__(message)
        self.issues = list(issues or [])
        self.dataset = dataset


def _valid_symbol(symbol) -> bool:
    return isinstance(symbol, str) and len(symbol) <= 32 and bool(_SYMBOL.fullmatch(symbol))


def _canonical(value):
    if isinstance(value, Decimal):
        # Decimal.normalize() depends on global precision; formatting does not.
        if not value.is_finite():
            return str(value)
        text = format(value, 'f')
        text = text.rstrip('0').rstrip('.') if '.' in text else text
        return '0' if value == 0 else text
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def dataset_version(dataset: Dataset) -> str:
    """SHA-256 of semantic content, including source, flags, actions and limits.

    Excludes the version field itself, paths and wall-clock times. Row ordering
    and Decimal scale do not affect identity; duplicate rows still count.
    """
    from dataclasses import asdict

    payload = _canonical(asdict(dataset))
    payload.pop('version')
    payload['bars'] = sorted(payload['bars'], key=_json)
    payload['actions'] = sorted(payload['actions'], key=_json)
    payload['limitations'] = sorted(payload['limitations'])
    payload['format'] = 'quantlab-semantic-v1'
    return 'sha256:' + hashlib.sha256(_json(payload).encode('utf-8')).hexdigest()


def _finite_decimal(value, *, positive=True) -> bool:
    return (isinstance(value, Decimal) and value.is_finite()
            and (value > 0 if positive else value >= 0))


def _utc(value) -> bool:
    if not isinstance(value, datetime):
        return False
    try:
        require_utc(value)
        return True
    except ValueError:
        return False


def _latest_completed(now: datetime) -> date | None:
    day = now.astimezone(ZoneInfo('America/New_York')).date()
    if not CALENDAR_START <= day <= CALENDAR_END:
        return None
    for _ in range(32):
        if day < CALENDAR_START:
            return None
        times = session_times(day)
        if times is not None and times[1] + timedelta(minutes=1) <= now:
            return day
        day -= timedelta(days=1)
    return None


def validate_dataset(dataset: Dataset, now: datetime | None = None) -> list[dict]:
    """Inspect without dropping, filling, deduplicating or correcting any data.

    Missing sessions, unexplained >40% jumps, five unchanged closes, zero volume
    and daily staleness are warnings (not proof of an error). Structural, OHLC,
    calendar, duplicate and premature-availability problems are errors. Missing
    sessions use the observed dataset-wide horizon, not a historical universe.
    Staleness uses the latest completed XNYS session plus a one-minute reporting
    grace, so a weekend/holiday does not by itself make Friday's data stale.
    Pass explicit historical UTC ``now`` for deterministic replay diagnostics.
    """
    now = datetime.now(timezone.utc) if now is None else now
    require_utc(now)
    issues: list[dict] = []

    def add(severity, code, message, symbol=None, **details):
        issue = {'severity': severity, 'code': code, 'message': message}
        if symbol is not None:
            issue['symbol'] = symbol
        issue.update(details)
        issues.append(issue)

    if type(dataset.synthetic) is not bool:
        add('error', 'INVALID_METADATA', 'synthetic must be an explicit boolean.')
    if not isinstance(dataset.source, str) or not dataset.source.strip():
        add('error', 'INVALID_METADATA', 'A nonempty source is required.')
    if not isinstance(dataset.limitations, list) or any(
            not isinstance(item, str) for item in dataset.limitations):
        add('error', 'INVALID_METADATA', 'limitations must be a list of strings.')
    if dataset.synthetic:
        add('warning', 'SYNTHETIC_DATA', 'SYNTHETIC software-test data cannot establish investment evidence.')
    if not dataset.bars:
        add('error', 'EMPTY_DATASET', 'No market bars were supplied.')
    if not isinstance(dataset.version, str):
        add('error', 'INVALID_METADATA', 'version must be a string.')
    elif not dataset.version.strip():
        add('error', 'MISSING_VERSION', 'Dataset has no version identifier.')
    elif dataset.version.startswith('sha256:'):
        try:
            content_version = dataset_version(dataset)
        except (TypeError, ValueError):
            # Invalid inputs must still reach their field-level diagnostics;
            # hashing must not turn a quality report into an exception.
            add('error', 'UNHASHABLE_CONTENT', 'Invalid content prevents semantic hash verification.')
        else:
            if dataset.version != content_version:
                add('error', 'VERSION_MISMATCH', 'Content no longer matches the dataset version hash.')
    else:
        add('warning', 'UNVERIFIED_VERSION', 'Version is not a QuantLab content hash.')

    grouped = defaultdict(list)
    seen = set()
    valid_days = set()
    for bar in dataset.bars:
        symbol = bar.symbol
        if not _valid_symbol(symbol):
            add('error', 'INVALID_SYMBOL', 'Expected a canonical uppercase US equity/ETF symbol.', symbol)
        if type(bar.session) is not date:
            add('error', 'INVALID_SESSION', 'Session must be an exchange date.', symbol)
            continue
        key = (symbol, bar.session)
        if key in seen:
            add('error', 'DUPLICATE_BAR', 'Duplicate symbol/session; no row was discarded.', symbol,
                session=bar.session.isoformat())
        seen.add(key)
        grouped[symbol].append(bar)
        try:
            times = session_times(bar.session)
        except ValueError:
            times = None
            add('error', 'CALENDAR_RANGE', 'Session is outside the supported XNYS calendar.', symbol)
        else:
            if times is None:
                add('error', 'NON_TRADING_SESSION', 'A bar falls on an XNYS closure.', symbol,
                    session=bar.session.isoformat())
            else:
                valid_days.add(bar.session)
        timestamps_valid = all(_utc(getattr(bar, field)) for field in ('open_at', 'close_at', 'available_at'))
        if not timestamps_valid:
            add('error', 'INVALID_TIMESTAMP', 'All bar timestamps must be timezone-aware UTC.', symbol)
        else:
            if bar.open_at >= bar.close_at:
                add('error', 'INVALID_TIME_ORDER', 'open_at must precede close_at.', symbol)
            if times and (bar.open_at, bar.close_at) != times:
                add('error', 'SESSION_TIME_MISMATCH', 'Open/close do not match XNYS (including DST/early close).', symbol)
            if bar.available_at < bar.close_at:
                add('error', 'PREMATURE_AVAILABILITY', 'A complete daily bar cannot be known before its close.', symbol)
            if bar.available_at > now:
                add('warning', 'FUTURE_AVAILABILITY', 'Observation is unavailable at the requested as-of time.', symbol)
            if bar.close_at > now:
                add('warning', 'FUTURE_BAR', 'Session has not completed at the requested as-of time.', symbol)
            if bar.available_at > bar.close_at + timedelta(days=1):
                add('warning', 'DELAYED_AVAILABILITY', 'Bar became available more than one day after close.', symbol)
        prices_valid = all(_finite_decimal(getattr(bar, field)) for field in _PRICE_FIELDS)
        if not prices_valid:
            add('error', 'INVALID_PRICE', 'Every raw/adjusted price must be a finite positive Decimal.', symbol)
        elif not (bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high):
            add('error', 'INVALID_OHLC', 'Raw OHLC envelope is inconsistent.', symbol)
        if type(bar.volume) is not int or bar.volume < 0:
            add('error', 'INVALID_VOLUME', 'Volume must be a nonnegative integer.', symbol)
        elif bar.volume == 0:
            add('warning', 'ZERO_VOLUME', 'Zero-volume bar must not be assumed executable.', symbol)

    action_by_key = {}
    for action in dataset.actions:
        symbol = action.symbol
        if not _valid_symbol(symbol):
            add('error', 'INVALID_SYMBOL', 'Invalid corporate-action symbol.', symbol)
        if type(action.ex_date) is not date:
            add('error', 'INVALID_ACTION_SESSION', 'Corporate action ex_date must be an exchange date.', symbol)
            continue
        key = (symbol, action.ex_date)
        if key in action_by_key:
            add('error', 'DUPLICATE_ACTION', 'Use one combined split/dividend action per symbol/ex_date.', symbol)
        action_by_key[key] = action
        if not _finite_decimal(action.split_ratio) or not _finite_decimal(action.cash_dividend, positive=False):
            add('error', 'INVALID_ACTION_VALUE', 'Split ratio must be positive; USD dividend nonnegative, both finite Decimal.', symbol)
        if not _utc(action.known_at):
            add('error', 'INVALID_ACTION_TIMESTAMP', 'known_at must be timezone-aware UTC.', symbol)
        else:
            if action.known_at > now:
                add('warning', 'FUTURE_ACTION_KNOWLEDGE', 'Corporate action must be gated out before known_at.', symbol)
            try:
                times = session_times(action.ex_date)
            except ValueError:
                times = None
            if times is None:
                add('error', 'INVALID_ACTION_SESSION', 'Corporate-action ex_date is not a supported XNYS session.', symbol)
            elif action.known_at > times[0]:
                add('warning', 'ACTION_KNOWN_LATE', 'Corporate action was not known at its ex-date open.', symbol)
        if key not in seen:
            add('warning', 'ORPHAN_ACTION', 'Corporate action has no corresponding bar; retained without inference.', symbol)

    expected_days = set()
    if valid_days:
        day, end = min(valid_days), max(valid_days)
        while day <= end:
            if session_times(day):
                expected_days.add(day)
            day += timedelta(days=1)
    latest = _latest_completed(now)
    if latest is None:
        add('warning', 'STALENESS_UNCHECKED', 'As-of date is outside supported calendar coverage.')
    for symbol, bars in sorted(grouped.items()):
        days = {bar.session for bar in bars}
        missing = sorted(expected_days - days)
        if missing:
            add('warning', 'MISSING_SESSIONS', 'Missing XNYS observations; no forward-fill or universe assumptions made.',
                symbol, count=len(missing), sessions=[day.isoformat() for day in missing])
        available = [bar for bar in bars if _utc(bar.available_at) and _utc(bar.close_at)
                     and bar.available_at <= now and bar.close_at <= now and bar.session in valid_days]
        if not available:
            add('warning', 'NO_AVAILABLE_DATA', 'No completed, available bar at the requested as-of time.', symbol)
        elif latest and max(bar.session for bar in available) < latest:
            add('warning', 'STALE_DATA', 'Latest available daily bar precedes the latest completed XNYS session.',
                symbol, latest_session=max(bar.session for bar in available).isoformat(),
                expected_session=latest.isoformat())
        ordered = sorted(bars, key=lambda bar: bar.session)
        flat = 1
        for previous, current in zip(ordered, ordered[1:]):
            if previous.session == current.session or not all(_finite_decimal(b.close) for b in (previous, current)):
                continue
            flat = flat + 1 if current.close == previous.close else 1
            if flat == 5:
                add('warning', 'UNCHANGED_PRICE', 'Five consecutive observed closes are unchanged; investigate stale/illiquid data.', symbol)
            action = action_by_key.get((symbol, current.session))
            split = action.split_ratio if action and _finite_decimal(action.split_ratio) else Decimal('1')
            dividend = action.cash_dividend if action and _finite_decimal(action.cash_dividend, positive=False) else Decimal('0')
            with localcontext() as ctx:
                ctx.prec = 38
                move = abs((current.close + dividend) * split / previous.close - 1)
            if move > Decimal('0.40'):
                add('warning', 'PRICE_JUMP', 'Greater than 40% close move after supplied action normalization; raw data retained.',
                    symbol, session=current.session.isoformat(), relative_move=str(move))
    return issues
