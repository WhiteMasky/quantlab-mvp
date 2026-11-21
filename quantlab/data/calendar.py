"""Offline XNYS sessions supplied by exchange_calendars, never weekday guesses."""
from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache

CALENDAR_START = date(1990, 1, 1)
CALENDAR_END = date(2035, 12, 31)
CALENDAR_LIMITATION = (
    'XNYS regular sessions only, 1990-2035, from installed exchange_calendars; '
    'future schedules are projections and may change with exceptional closures. '
    'Extended hours, instrument-specific halts and venue differences are not represented.'
)


@lru_cache(maxsize=1)
def _calendar():
    import exchange_calendars

    # Fixed bounds avoid the dependency's wall-clock-dependent default window.
    # Padding lets holidays at the supported endpoints return None normally.
    return exchange_calendars.get_calendar(
        'XNYS', start='1989-12-01', end='2036-01-31'
    )


@lru_cache(maxsize=32768)
def session_times(day: date) -> tuple[datetime, datetime] | None:
    """Return actual UTC open/close (including DST/early closes), or None.

    Only dates within 1990-2035 are supported; unsupported dates raise instead
    of being silently interpreted as holidays. No network request is made.
    """
    if type(day) is not date:
        raise TypeError('day must be a datetime.date, not a datetime')
    if not CALENDAR_START <= day <= CALENDAR_END:
        raise ValueError('XNYS calendar supports dates from 1990 through 2035')
    calendar = _calendar()
    label = day.isoformat()
    if not calendar.is_session(label):
        return None
    return (
        calendar.session_open(label).to_pydatetime().astimezone(timezone.utc),
        calendar.session_close(label).to_pydatetime().astimezone(timezone.utc),
    )
