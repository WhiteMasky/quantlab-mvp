from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_UP, localcontext

import pytest

from quantlab.data import (
    DataSource, SyntheticDataSource, dataset_version, generate_synthetic,
    session_times, validate_dataset,
)
from quantlab.models import jsonable


@pytest.mark.parametrize('day,opening,closing', [
    ('2024-03-08', '14:30', '21:00'),
    ('2024-03-11', '13:30', '20:00'),
    ('2024-11-01', '13:30', '20:00'),
    ('2024-11-04', '14:30', '21:00'),
    ('2024-07-03', '13:30', '17:00'),
    ('2024-11-29', '14:30', '18:00'),
])
def test_actual_xnys_times_include_dst_and_early_closes(day, opening, closing):
    assert session_times(date.fromisoformat(day)) == (
        datetime.fromisoformat(f'{day}T{opening}:00+00:00'),
        datetime.fromisoformat(f'{day}T{closing}:00+00:00'),
    )


@pytest.mark.parametrize('day', ['2024-03-09', '2024-07-04', '2024-03-29', '2001-09-11', '2012-10-29', '1990-01-01'])
def test_weekends_holidays_and_exceptional_closures(day):
    assert session_times(date.fromisoformat(day)) is None


@pytest.mark.parametrize('day', [date(1989, 12, 31), date(2036, 1, 1)])
def test_calendar_outside_supported_range_is_not_a_holiday(day):
    with pytest.raises(ValueError, match='1990 through 2035'):
        session_times(day)


@pytest.mark.parametrize('day', ['2024-03-08', datetime(2024, 3, 8), None])
def test_calendar_requires_exchange_date(day):
    with pytest.raises(TypeError, match='datetime.date'):
        session_times(day)


def test_default_synthetic_stable_hash_and_clean_quality():
    dataset = generate_synthetic()
    assert dataset == generate_synthetic(symbols=('TLT', 'IWM', 'QQQ', 'SPY'))
    assert dataset.version == 'sha256:26440a820d6285879077b15a576107c4e1537dd352902c1349052c5675ed2fe6'
    assert len(dataset.bars) == 504 * 4
    assert len({bar.session for bar in dataset.bars}) == 504
    assert min(bar.session for bar in dataset.bars) == date(2022, 1, 3)
    assert dataset.synthetic is True
    assert 'SYNTHETIC' in dataset.source
    assert any('not guaranteed point-in-time' in text for text in dataset.limitations)
    assert any('delistings' in text for text in dataset.limitations)
    assert any('Not actual' in text for text in dataset.limitations)
    now = max(bar.available_at for bar in dataset.bars)
    assert [issue['code'] for issue in validate_dataset(dataset, now)] == ['SYNTHETIC_DATA']
    assert all(bar.available_at > bar.close_at and bar.open_at.tzinfo == timezone.utc for bar in dataset.bars)


def test_seed_and_decimal_context_determinism():
    expected = generate_synthetic(seed=42, sessions=80)
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.rounding = ROUND_UP
        assert generate_synthetic(seed=42, sessions=80) == expected
        assert dataset_version(expected) == expected.version
    assert generate_synthetic(seed=43, sessions=80).version != expected.version
    adapter = SyntheticDataSource(seed=42, sessions=80)
    assert isinstance(adapter, DataSource)
    assert adapter.load() == expected
    with pytest.raises(ValueError, match='does not read'):
        adapter.load('unused.csv')


def test_future_adjustments_never_rewrite_raw_ohlc():
    short = generate_synthetic(sessions=63, symbols=('SPY',))
    long = generate_synthetic(sessions=260, symbols=('SPY',))
    for left, right in zip(short.bars, long.bars):
        assert replace(left, adjusted_close=right.adjusted_close) == right
    assert short.bars[0].adjusted_close != long.bars[0].adjusted_close
    split = next(action for action in long.actions if action.split_ratio == 2)
    index = next(index for index, bar in enumerate(long.bars) if bar.session == split.ex_date)
    previous, current = long.bars[index - 1:index + 1]
    assert Decimal('0.45') < current.open / previous.close < Decimal('0.55')
    assert Decimal('0.95') < current.adjusted_close / previous.adjusted_close < Decimal('1.05')
    assert split.known_at < current.open_at
    assert split.cash_dividend == Decimal('0.15')
    assert long.bars[-1].adjusted_close == long.bars[-1].close


@pytest.mark.parametrize('kwargs', [
    {'seed': True}, {'seed': '7'}, {'sessions': 0}, {'sessions': -1},
    {'sessions': True}, {'sessions': 1.5}, {'sessions': 10001},
    {'symbols': ()}, {'symbols': 'SPY'}, {'symbols': ('spy',)},
    {'symbols': ('SPY', 'SPY')}, {'symbols': (' SPY',)}, {'symbols': (None,)},
])
def test_invalid_synthetic_arguments(kwargs):
    with pytest.raises(ValueError):
        generate_synthetic(**kwargs)


def test_requested_synthetic_history_must_fit_calendar():
    with pytest.raises(ValueError, match='exceeds supported'):
        generate_synthetic(sessions=10000, symbols=('SPY',))


def test_semantic_hash_ignores_order_and_decimal_scale(sample_dataset):
    expected = sample_dataset.version
    sample_dataset.bars.reverse()
    sample_dataset.actions.reverse()
    sample_dataset.limitations.reverse()
    bar = sample_dataset.bars[0]
    sample_dataset.bars[0] = replace(bar, open=Decimal(str(bar.open) + '00'))
    assert dataset_version(sample_dataset) == expected
    sample_dataset.version = 'vendor-label'
    assert dataset_version(sample_dataset) == expected
    assert jsonable(sample_dataset)['version'] == 'vendor-label'


@pytest.mark.parametrize('change', ['source', 'synthetic', 'limitations', 'actions', 'availability', 'adjusted', 'duplicate'])
def test_semantic_hash_covers_provenance_actions_and_observations(sample_dataset, change):
    original = sample_dataset.version
    if change == 'source':
        sample_dataset.source += ' corrected'
    elif change == 'synthetic':
        sample_dataset.synthetic = False
    elif change == 'limitations':
        sample_dataset.limitations.append('Additional disclosure')
    elif change == 'actions':
        sample_dataset.actions[0] = replace(sample_dataset.actions[0], cash_dividend=Decimal('0.75'))
    elif change == 'availability':
        sample_dataset.bars[0] = replace(sample_dataset.bars[0], available_at=sample_dataset.bars[-1].available_at)
    elif change == 'adjusted':
        sample_dataset.bars[0] = replace(sample_dataset.bars[0], adjusted_close=Decimal('1'))
    else:
        sample_dataset.bars.append(sample_dataset.bars[0])
    assert dataset_version(sample_dataset) != original
