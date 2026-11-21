from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantlab.data import dataset_version, generate_synthetic, session_times, validate_dataset
from quantlab.models import jsonable


def codes(dataset, now):
    dataset.version = dataset_version(dataset)
    return {issue['code'] for issue in validate_dataset(dataset, now)}


def test_validation_is_non_destructive(sample_dataset, as_of):
    before = deepcopy(sample_dataset)
    issues = validate_dataset(sample_dataset, as_of)
    assert [issue['code'] for issue in issues] == ['SYNTHETIC_DATA']
    assert all({'severity', 'code', 'message'} <= issue.keys() for issue in issues)
    assert sample_dataset == before
    assert jsonable(sample_dataset) == jsonable(before)


@pytest.mark.parametrize('field,value,expected', [
    ('open', Decimal('0'), 'INVALID_PRICE'),
    ('close', Decimal('-1'), 'INVALID_PRICE'),
    ('adjusted_close', Decimal('NaN'), 'INVALID_PRICE'),
    ('high', Decimal('Infinity'), 'INVALID_PRICE'),
    ('low', Decimal('101'), 'INVALID_OHLC'),
    ('high', Decimal('99'), 'INVALID_OHLC'),
    ('close', 100.0, 'INVALID_PRICE'),
    ('volume', -1, 'INVALID_VOLUME'),
    ('volume', 1.5, 'INVALID_VOLUME'),
    ('volume', True, 'INVALID_VOLUME'),
    ('volume', 0, 'ZERO_VOLUME'),
    ('symbol', 'spy', 'INVALID_SYMBOL'),
    ('session', '2024-03-08', 'INVALID_SESSION'),
])
def test_bar_quality_errors_are_diagnosed(sample_dataset, as_of, field, value, expected):
    sample_dataset.bars[0] = replace(sample_dataset.bars[0], **{field: value})
    assert expected in codes(sample_dataset, as_of)


def test_adjusted_close_is_not_part_of_executable_ohlc_envelope(sample_dataset, as_of):
    sample_dataset.bars[0] = replace(sample_dataset.bars[0], adjusted_close=Decimal('1.234567890123456789'))
    assert 'INVALID_OHLC' not in codes(sample_dataset, as_of)
    assert not any(issue['severity'] == 'error' for issue in validate_dataset(sample_dataset, as_of))


def test_duplicate_bars_and_actions_are_retained(sample_dataset, as_of):
    sample_dataset.bars.append(sample_dataset.bars[0])
    sample_dataset.actions.append(sample_dataset.actions[0])
    assert {'DUPLICATE_BAR', 'DUPLICATE_ACTION'} <= codes(sample_dataset, as_of)
    assert len(sample_dataset.bars) == 7
    assert len(sample_dataset.actions) == 3


@pytest.mark.parametrize('field,value,expected', [
    ('session', date(2024, 3, 9), 'NON_TRADING_SESSION'),
    ('session', date(2036, 1, 2), 'CALENDAR_RANGE'),
])
def test_invalid_exchange_sessions(sample_dataset, as_of, field, value, expected):
    sample_dataset.bars[0] = replace(sample_dataset.bars[0], **{field: value})
    assert expected in codes(sample_dataset, as_of)


def test_calendar_time_mismatch_and_premature_availability(sample_dataset, as_of):
    bar = sample_dataset.bars[0]
    sample_dataset.bars[0] = replace(bar, open_at=bar.open_at + timedelta(hours=1), available_at=bar.open_at)
    assert {'SESSION_TIME_MISMATCH', 'PREMATURE_AVAILABILITY'} <= codes(sample_dataset, as_of)
    sample_dataset.bars[0] = replace(bar, open_at=bar.close_at)
    assert 'INVALID_TIME_ORDER' in codes(sample_dataset, as_of)


def test_as_of_availability_and_staleness_are_independent_of_session(sample_dataset, as_of):
    for index, bar in enumerate(sample_dataset.bars):
        if bar.symbol == 'SPY':
            sample_dataset.bars[index] = replace(bar, available_at=as_of + timedelta(days=2))
    issues = validate_dataset(sample_dataset, as_of)
    assert any(issue['code'] == 'NO_AVAILABLE_DATA' and issue['symbol'] == 'SPY' for issue in issues)
    assert any(issue['code'] == 'FUTURE_AVAILABILITY' for issue in issues)
    assert any(issue['code'] == 'DELAYED_AVAILABILITY' for issue in issues)
    # An observation can exist for today's completed session but remain unpublished.
    sample_dataset.bars[0] = replace(sample_dataset.bars[0], available_at=sample_dataset.bars[0].close_at)
    assert 'STALE_DATA' in codes(sample_dataset, as_of)


def test_future_bar_and_corporate_action_are_flagged(sample_dataset):
    now = sample_dataset.bars[0].open_at - timedelta(seconds=1)
    assert {'FUTURE_BAR', 'FUTURE_AVAILABILITY', 'FUTURE_ACTION_KNOWLEDGE', 'NO_AVAILABLE_DATA'} <= codes(sample_dataset, now)


def test_weekend_and_reporting_grace_do_not_make_latest_daily_bar_stale(sample_dataset):
    sample_dataset.bars = sample_dataset.bars[:2]
    sample_dataset.actions = []
    friday = sample_dataset.bars[0]
    sunday = friday.close_at + timedelta(days=2)
    assert 'STALE_DATA' not in codes(sample_dataset, sunday)
    monday_close = session_times(date(2024, 3, 11))[1]
    assert 'STALE_DATA' not in codes(sample_dataset, monday_close + timedelta(seconds=59))
    assert 'STALE_DATA' in codes(sample_dataset, monday_close + timedelta(minutes=1))


def test_missing_sessions_no_forward_fill(sample_dataset, as_of):
    sample_dataset.bars = [bar for bar in sample_dataset.bars
                           if not (bar.symbol == 'SPY' and bar.session == date(2024, 3, 11))]
    before = list(sample_dataset.bars)
    issues = validate_dataset(sample_dataset, as_of)
    gap = next(issue for issue in issues if issue['code'] == 'MISSING_SESSIONS')
    assert gap['symbol'] == 'SPY'
    assert gap['count'] == 1 and gap['sessions'] == ['2024-03-11']
    assert any(issue['code'] == 'ORPHAN_ACTION' for issue in issues)
    assert sample_dataset.bars == before


def test_explained_split_dividend_is_not_an_unexplained_jump(sample_dataset, as_of):
    assert 'PRICE_JUMP' not in codes(sample_dataset, as_of)
    sample_dataset.actions = []
    assert 'PRICE_JUMP' in codes(sample_dataset, as_of)


def test_five_flat_closes_are_only_a_warning():
    dataset = generate_synthetic(sessions=6, symbols=('SPY',))
    dataset.bars = [replace(bar, open=Decimal('100'), high=Decimal('101'), low=Decimal('99'),
                            close=Decimal('100'), adjusted_close=Decimal('100')) for bar in dataset.bars]
    now = dataset.bars[-1].available_at
    assert 'UNCHANGED_PRICE' in codes(dataset, now)
    issue = next(issue for issue in validate_dataset(dataset, now) if issue['code'] == 'UNCHANGED_PRICE')
    assert issue['severity'] == 'warning'


@pytest.mark.parametrize('field,value,expected', [
    ('split_ratio', Decimal('0'), 'INVALID_ACTION_VALUE'),
    ('split_ratio', Decimal('NaN'), 'INVALID_ACTION_VALUE'),
    ('cash_dividend', Decimal('-0.01'), 'INVALID_ACTION_VALUE'),
    ('cash_dividend', Decimal('Infinity'), 'INVALID_ACTION_VALUE'),
    ('ex_date', date(2024, 3, 9), 'INVALID_ACTION_SESSION'),
    ('symbol', 'qqq', 'INVALID_SYMBOL'),
])
def test_corporate_action_quality(sample_dataset, as_of, field, value, expected):
    sample_dataset.actions[0] = replace(sample_dataset.actions[0], **{field: value})
    assert expected in codes(sample_dataset, as_of)


def test_naive_action_time_is_not_silently_assigned_utc(sample_dataset, as_of):
    action = sample_dataset.actions[0]
    sample_dataset.actions[0] = replace(action, known_at=action.known_at.replace(tzinfo=None))
    assert 'INVALID_ACTION_TIMESTAMP' in codes(sample_dataset, as_of)


def test_naive_as_of_is_rejected(sample_dataset, as_of):
    with pytest.raises(ValueError, match='UTC'):
        validate_dataset(sample_dataset, as_of.replace(tzinfo=None))


def test_version_mismatch_missing_version_and_vendor_label(sample_dataset, as_of):
    sample_dataset.source += ' altered'
    assert 'VERSION_MISMATCH' in {issue['code'] for issue in validate_dataset(sample_dataset, as_of)}
    sample_dataset.version = ''
    assert 'MISSING_VERSION' in {issue['code'] for issue in validate_dataset(sample_dataset, as_of)}
    sample_dataset.version = 'vendor-release'
    assert 'UNVERIFIED_VERSION' in {issue['code'] for issue in validate_dataset(sample_dataset, as_of)}


@pytest.mark.parametrize('field,value', [('synthetic', 'true'), ('source', ''), ('version', 123), ('limitations', [123, 'text'])])
def test_invalid_metadata_is_reported_not_crashed(sample_dataset, as_of, field, value):
    setattr(sample_dataset, field, value)
    assert 'INVALID_METADATA' in {issue['code'] for issue in validate_dataset(sample_dataset, as_of)}


def test_nonfinite_float_price_is_reported_not_crashed(sample_dataset, as_of):
    sample_dataset.bars[0] = replace(sample_dataset.bars[0], open=float('nan'))
    assert 'INVALID_PRICE' in {issue['code'] for issue in validate_dataset(sample_dataset, as_of)}


def test_empty_dataset_reports_error(sample_dataset, as_of):
    sample_dataset.bars = []
    assert 'EMPTY_DATASET' in codes(sample_dataset, as_of)
