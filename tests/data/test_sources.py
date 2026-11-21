import csv
import io
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from quantlab.data import (
    CSVDataSource, DataIntegrityError, DataSource, cache_dataset,
    dataset_version, read_dataset, validate_dataset,
)
from quantlab.data.sources import CSV_ACTION_FIELDS, CSV_BAR_FIELDS
from quantlab.models import jsonable


def csv_text(rows, header=CSV_BAR_FIELDS):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(field, '') for field in header])
    return output.getvalue()


@pytest.fixture
def bar_row(sample_dataset):
    return jsonable(sample_dataset.bars[0])


@pytest.fixture
def load_csv(tmp_path):
    def load(text, **options):
        path = tmp_path / 'prices.csv'
        path.write_text(text, encoding='utf-8', newline='')
        return CSVDataSource(**options).load(path)
    return load


def test_strict_csv_raw_adjusted_actions_and_provenance(sample_dataset, load_csv, tmp_path):
    rows = jsonable(sample_dataset.bars)
    for row in rows:
        action = next((action for action in jsonable(sample_dataset.actions)
                       if action['symbol'] == row['symbol'] and action['ex_date'] == row['session']), None)
        if action:
            row.update(action)
    # Header order and a UTF-8 BOM are supported explicitly.
    header = tuple(reversed(CSV_BAR_FIELDS + CSV_ACTION_FIELDS))
    dataset = load_csv('\ufeff' + csv_text(rows, header), synthetic=True, source='Fixture CSV')
    assert dataset.bars == sample_dataset.bars
    assert dataset.actions == sample_dataset.actions
    assert dataset.bars[0].close == Decimal('100.0000')
    assert dataset.bars[0].adjusted_close == Decimal('50')
    assert dataset.version == dataset_version(dataset)
    assert dataset.source == 'Fixture CSV' and dataset.synthetic is True
    assert any('not verified' in text for text in dataset.limitations)
    assert read_dataset(cache_dataset(dataset, tmp_path / 'imported.parquet')) == dataset
    assert isinstance(CSVDataSource(), DataSource)


def test_no_action_values_are_not_inferred_and_default_source_is_unverified(bar_row, load_csv):
    dataset = load_csv(csv_text([bar_row], CSV_BAR_FIELDS + CSV_ACTION_FIELDS))
    assert dataset.actions == []
    assert dataset.synthetic is False
    assert 'unverified' in dataset.source
    assert dataset.bars[0].open == Decimal(bar_row['open'])
    assert dataset.bars[0].adjusted_close == Decimal(bar_row['adjusted_close'])


@pytest.mark.parametrize('field', ['open', 'high', 'low', 'close', 'adjusted_close', 'available_at', 'session'])
def test_required_columns_never_inferred(bar_row, load_csv, field):
    header = tuple(name for name in CSV_BAR_FIELDS if name != field)
    with pytest.raises(DataIntegrityError) as exc:
        load_csv(csv_text([bar_row], header))
    assert exc.value.issues[0]['code'] == 'CSV_FORMAT'


@pytest.mark.parametrize('header', [
    CSV_BAR_FIELDS + ('close',), CSV_BAR_FIELDS + ('adj_close',),
    CSV_BAR_FIELDS + ('split_ratio',), (' symbol',) + CSV_BAR_FIELDS[1:], (),
])
def test_duplicate_unknown_partial_and_whitespace_headers(bar_row, load_csv, header):
    with pytest.raises(DataIntegrityError):
        load_csv(csv_text([bar_row], header))


@pytest.mark.parametrize('field,value', [
    ('symbol', 'spy'), ('symbol', ' SPY'), ('symbol', ''),
    ('open', ''), ('open', ' 100'), ('open', '100 '), ('open', 'NaN'),
    ('open', 'Infinity'), ('open', '-1'), ('open', '+1'), ('open', '1e2'), ('open', '01'),
    ('adjusted_close', ''), ('adjusted_close', 'inf'), ('volume', '1.0'),
    ('volume', '-1'), ('volume', '01'), ('volume', ''),
    ('session', '2024-3-8'), ('session', '2024-02-30'),
    ('available_at', '2024-03-08T21:01:00'),
    ('available_at', '2024-03-08T16:01:00-05:00'),
    ('available_at', '2024-03-08T21:01Z'),
])
def test_lexical_errors_fail_without_coercion(bar_row, load_csv, field, value):
    bar_row[field] = value
    with pytest.raises(DataIntegrityError) as exc:
        load_csv(csv_text([bar_row]))
    assert exc.value.issues[0]['code'] == 'CSV_FORMAT'
    assert exc.value.issues[0]['row'] == 2


@pytest.mark.parametrize('field,value,expected', [
    ('open', '0', 'INVALID_PRICE'), ('adjusted_close', '0', 'INVALID_PRICE'),
    ('low', '101', 'INVALID_OHLC'), ('high', '99', 'INVALID_OHLC'),
    ('available_at', '2024-03-08T20:59:59Z', 'PREMATURE_AVAILABILITY'),
    ('open_at', '2024-03-08T13:30:00Z', 'SESSION_TIME_MISMATCH'),
    ('session', '2024-03-09', 'NON_TRADING_SESSION'),
])
def test_semantic_errors_fail_with_full_parsed_dataset(bar_row, load_csv, field, value, expected):
    bar_row[field] = value
    with pytest.raises(DataIntegrityError) as exc:
        load_csv(csv_text([bar_row]))
    assert expected in {issue['code'] for issue in exc.value.issues}
    assert len(exc.value.dataset.bars) == 1


def test_duplicates_not_silently_dropped(bar_row, load_csv):
    with pytest.raises(DataIntegrityError) as exc:
        load_csv(csv_text([bar_row, bar_row]))
    assert 'DUPLICATE_BAR' in {issue['code'] for issue in exc.value.issues}
    assert len(exc.value.dataset.bars) == 2


@pytest.mark.parametrize('action', [
    {'split_ratio': '2'},
    {'ex_date': '2024-03-11', 'known_at': '2024-03-07T21:01:00Z', 'split_ratio': '2', 'cash_dividend': '0'},
    {'ex_date': '2024-03-08', 'known_at': '2024-03-07T21:01:00', 'split_ratio': '2', 'cash_dividend': '0'},
])
def test_action_fields_are_all_or_nothing_explicit_utc_and_same_session(bar_row, load_csv, action):
    bar_row.update(action)
    with pytest.raises(DataIntegrityError) as exc:
        load_csv(csv_text([bar_row], CSV_BAR_FIELDS + CSV_ACTION_FIELDS))
    assert exc.value.issues[0]['code'] == 'CSV_FORMAT'


def test_invalid_split_and_duplicate_action_rejected(bar_row, load_csv):
    bar_row.update(ex_date=bar_row['session'], known_at='2024-03-07T21:01:00Z', split_ratio='0', cash_dividend='0')
    with pytest.raises(DataIntegrityError) as exc:
        load_csv(csv_text([bar_row], CSV_BAR_FIELDS + CSV_ACTION_FIELDS))
    assert 'INVALID_ACTION_VALUE' in {issue['code'] for issue in exc.value.issues}
    bar_row['split_ratio'] = '2'
    with pytest.raises(DataIntegrityError) as exc:
        load_csv(csv_text([bar_row, bar_row], CSV_BAR_FIELDS + CSV_ACTION_FIELDS))
    assert 'DUPLICATE_ACTION' in {issue['code'] for issue in exc.value.issues}
    assert len(exc.value.dataset.actions) == 2


def test_future_zero_volume_and_gap_warnings_do_not_drop_rows(sample_dataset, as_of, load_csv):
    rows = [jsonable(replace(bar, volume=0, available_at=as_of + timedelta(days=1)))
            for bar in sample_dataset.bars if bar.session != sample_dataset.bars[2].session]
    dataset = load_csv(csv_text(rows))
    assert len(dataset.bars) == 4
    codes = {issue['code'] for issue in validate_dataset(dataset, as_of)}
    assert {'MISSING_SESSIONS', 'ZERO_VOLUME', 'FUTURE_AVAILABILITY', 'NO_AVAILABLE_DATA'} <= codes


@pytest.mark.parametrize('kind', ['short', 'long', 'blank', 'quote', 'empty'])
def test_malformed_csv_rows_and_empty_file(bar_row, load_csv, kind):
    text = csv_text([bar_row])
    if kind == 'short':
        text = text.rsplit(',', 1)[0] + '\n'
    elif kind == 'long':
        text = text.rstrip('\r\n') + ',extra\n'
    elif kind == 'blank':
        text += '\n'
    elif kind == 'quote':
        text = ','.join(CSV_BAR_FIELDS) + '\n"unfinished'
    else:
        text = ''
    with pytest.raises(DataIntegrityError):
        load_csv(text)


def test_header_only_is_empty_dataset(load_csv):
    with pytest.raises(DataIntegrityError) as exc:
        load_csv(csv_text([]))
    assert 'EMPTY_DATASET' in {issue['code'] for issue in exc.value.issues}


def test_invalid_utf8_and_missing_csv_path(tmp_path):
    path = tmp_path / 'invalid.csv'
    path.write_bytes(b'\xff\xfe\x00\x00')
    with pytest.raises(DataIntegrityError):
        CSVDataSource().load(path)
    with pytest.raises(ValueError, match='required'):
        CSVDataSource().load()
    with pytest.raises(FileNotFoundError):
        CSVDataSource().load(tmp_path / 'missing.csv')


@pytest.mark.parametrize('options', [{'synthetic': 'true'}, {'synthetic': 1}, {'source': ''}, {'source': None}])
def test_provenance_must_be_explicit_valid_types(options):
    with pytest.raises(ValueError):
        CSVDataSource(**options)
