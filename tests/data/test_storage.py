import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quantlab.data import (
    DataIntegrityError, DataSource, ParquetDataSource, cache_dataset,
    dataset_version, generate_synthetic, read_dataset, validate_dataset,
)
from quantlab.data import storage
from quantlab.models import jsonable


def rewrite_metadata(path, mutate):
    table = pq.ParquetFile(path).read()
    metadata = dict(table.schema.metadata)
    payload = json.loads(metadata[b'quantlab.dataset'])
    mutate(payload)
    metadata[b'quantlab.dataset'] = json.dumps(payload).encode('utf-8')
    pq.write_table(table.replace_schema_metadata(metadata), path)


def test_lossless_single_file_roundtrip(sample_dataset, as_of, tmp_path):
    before = jsonable(sample_dataset)
    path = tmp_path / 'cache with spaces' / 'market.parquet'
    assert cache_dataset(sample_dataset, str(path)) == path
    assert path.read_bytes()[:4] == b'PAR1'
    assert list(path.parent.iterdir()) == [path]
    loaded = read_dataset(str(path))
    assert loaded == sample_dataset
    assert jsonable(loaded) == before == jsonable(sample_dataset)
    assert loaded.version == dataset_version(loaded)
    assert validate_dataset(loaded, as_of) == validate_dataset(sample_dataset, as_of)
    assert loaded.bars[0].open != loaded.bars[0].adjusted_close
    assert str(loaded.actions[0].split_ratio) == '2.0000'
    assert loaded.bars[0].available_at.microsecond == 123456
    adapter = ParquetDataSource()
    assert isinstance(adapter, DataSource)
    assert adapter.load(path) == sample_dataset
    with pytest.raises(ValueError, match='required'):
        adapter.load()


def test_default_synthetic_roundtrip(tmp_path):
    dataset = generate_synthetic()
    loaded = read_dataset(cache_dataset(dataset, tmp_path / 'default.parquet'))
    assert loaded == dataset
    assert len(loaded.bars) == 2016
    assert len(loaded.actions) == 28
    assert loaded.synthetic is True


def test_decimal_precision_scale_and_context_are_preserved(sample_dataset, tmp_path):
    value = Decimal('0.1234567890123456789012345678901234567890123456789000')
    sample_dataset.bars[0] = replace(sample_dataset.bars[0], adjusted_close=value)
    sample_dataset.version = dataset_version(sample_dataset)
    with localcontext() as ctx:
        ctx.prec = 6
        loaded = read_dataset(cache_dataset(sample_dataset, tmp_path / 'precise.parquet'))
    assert loaded == sample_dataset
    assert str(loaded.bars[0].adjusted_close) == str(value)


def test_copy_file_without_sidecars_and_preserve_order(sample_dataset, tmp_path):
    sample_dataset.bars.reverse()
    sample_dataset.actions.reverse()
    sample_dataset.limitations.reverse()
    original = cache_dataset(sample_dataset, tmp_path / 'first.parquet')
    copy = tmp_path / 'copy.parquet'
    copy.write_bytes(original.read_bytes())
    assert read_dataset(copy) == sample_dataset


def test_vendor_version_is_preserved_but_content_still_checked(sample_dataset, tmp_path):
    sample_dataset.version = 'vendor-release-2024-03'
    path = cache_dataset(sample_dataset, tmp_path / 'vendor.parquet')
    assert read_dataset(path) == sample_dataset
    rewrite_metadata(path, lambda payload: payload.update(source='different provider'))
    with pytest.raises(DataIntegrityError) as exc:
        read_dataset(path)
    assert exc.value.issues[0]['code'] == 'CACHE_HASH_MISMATCH'


def test_future_availability_and_action_knowledge_survive(sample_dataset, as_of, tmp_path):
    sample_dataset.bars[0] = replace(sample_dataset.bars[0], available_at=as_of + timedelta(days=1))
    sample_dataset.actions[0] = replace(sample_dataset.actions[0], known_at=as_of + timedelta(hours=1))
    sample_dataset.version = dataset_version(sample_dataset)
    loaded = read_dataset(cache_dataset(sample_dataset, tmp_path / 'future.parquet'))
    assert loaded == sample_dataset
    codes = {issue['code'] for issue in validate_dataset(loaded, as_of)}
    assert {'FUTURE_AVAILABILITY', 'FUTURE_ACTION_KNOWLEDGE', 'ACTION_KNOWN_LATE'} <= codes


@pytest.mark.parametrize('field', ['source', 'synthetic', 'limitations', 'actions', 'content_hash', 'version'])
def test_metadata_tampering_is_rejected(sample_dataset, tmp_path, field):
    path = cache_dataset(sample_dataset, tmp_path / 'tampered.parquet')
    replacements = {
        'source': 'another source', 'synthetic': False, 'limitations': [],
        'actions': [], 'content_hash': 'sha256:' + '0' * 64, 'version': 'sha256:' + '1' * 64,
    }
    rewrite_metadata(path, lambda payload: payload.update({field: replacements[field]}))
    with pytest.raises(DataIntegrityError) as exc:
        read_dataset(path)
    assert {issue['code'] for issue in exc.value.issues} & {'CACHE_HASH_MISMATCH', 'VERSION_MISMATCH'}


@pytest.mark.parametrize('field,value', [
    ('open', '99.5000'), ('adjusted_close', '49.5000'), ('volume', 42),
])
def test_bar_tampering_is_rejected(sample_dataset, tmp_path, field, value):
    path = cache_dataset(sample_dataset, tmp_path / 'tampered.parquet')
    table = pq.ParquetFile(path).read()
    rows = table.to_pylist()
    rows[0][field] = value
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    with pytest.raises(DataIntegrityError) as exc:
        read_dataset(path)
    assert exc.value.issues[0]['code'] == 'CACHE_HASH_MISMATCH'
    assert exc.value.dataset is not None


@pytest.mark.parametrize('field,value', [
    ('format', 'quantlab-parquet-v999'), ('synthetic', 'false'),
    ('limitations', None), ('actions', None), ('actions', [{}]),
    ('source', ''), ('version', None),
])
def test_malformed_metadata_is_rejected(sample_dataset, tmp_path, field, value):
    path = cache_dataset(sample_dataset, tmp_path / 'bad-metadata.parquet')
    rewrite_metadata(path, lambda payload: payload.update({field: value}))
    with pytest.raises(DataIntegrityError) as exc:
        read_dataset(path)
    assert exc.value.issues[0]['code'] == 'INVALID_CACHE'


@pytest.mark.parametrize('change', ['missing_metadata', 'missing_actions', 'schema', 'null', 'broken_json'])
def test_invalid_cache_structure_is_rejected(sample_dataset, tmp_path, change):
    path = cache_dataset(sample_dataset, tmp_path / 'bad.parquet')
    table = pq.ParquetFile(path).read()
    if change == 'missing_actions':
        rewrite_metadata(path, lambda payload: payload.pop('actions'))
    else:
        if change == 'missing_metadata':
            table = table.replace_schema_metadata(None)
        elif change == 'schema':
            table = table.drop(['adjusted_close'])
        elif change == 'null':
            index = table.schema.get_field_index('open')
            table = table.set_column(index, pa.field('open', pa.string()),
                                     pa.array([None] * table.num_rows, type=pa.string()))
        else:
            table = table.replace_schema_metadata({b'quantlab.dataset': b'not json'})
        pq.write_table(table, path)
    with pytest.raises(DataIntegrityError):
        read_dataset(path)


def test_corrupt_and_missing_files(tmp_path):
    path = tmp_path / 'broken.parquet'
    path.write_bytes(b'not a parquet file')
    with pytest.raises(DataIntegrityError):
        read_dataset(path)
    with pytest.raises(FileNotFoundError):
        read_dataset(tmp_path / 'absent.parquet')


@pytest.mark.parametrize('kind', ['duplicate', 'stale_hash', 'premature', 'bad_ohlc', 'bad_action'])
def test_invalid_dataset_cannot_replace_cache(sample_dataset, tmp_path, kind):
    path = cache_dataset(sample_dataset, tmp_path / 'unchanged.parquet')
    before = path.read_bytes()
    invalid = deepcopy(sample_dataset)
    if kind == 'duplicate':
        invalid.bars.append(invalid.bars[0])
    elif kind == 'stale_hash':
        invalid.source = 'changed without updating version'
    elif kind == 'premature':
        invalid.bars[0] = replace(invalid.bars[0], available_at=invalid.bars[0].open_at)
    elif kind == 'bad_ohlc':
        invalid.bars[0] = replace(invalid.bars[0], high=Decimal('1'))
    else:
        invalid.actions[0] = replace(invalid.actions[0], split_ratio=Decimal('0'))
    if kind != 'stale_hash':
        invalid.version = dataset_version(invalid)
    with pytest.raises(DataIntegrityError) as exc:
        cache_dataset(invalid, path)
    assert exc.value.dataset is invalid
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_even_rehashed_invalid_cache_fails_quality_checks(sample_dataset, tmp_path):
    path = cache_dataset(sample_dataset, tmp_path / 'invalid.parquet')
    invalid = deepcopy(sample_dataset)
    invalid.bars[0] = replace(invalid.bars[0], available_at=invalid.bars[0].open_at)
    invalid.version = dataset_version(invalid)
    table = pq.ParquetFile(path).read()
    rows = table.to_pylist()
    rows[0]['available_at'] = invalid.bars[0].available_at
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    rewrite_metadata(path, lambda payload: payload.update(version=invalid.version, content_hash=invalid.version))
    with pytest.raises(DataIntegrityError) as exc:
        read_dataset(path)
    assert 'PREMATURE_AVAILABILITY' in {issue['code'] for issue in exc.value.issues}


@pytest.mark.parametrize('stage', ['write', 'replace'])
def test_failed_write_keeps_previous_cache_and_cleans_temp(sample_dataset, tmp_path, monkeypatch, stage):
    path = cache_dataset(sample_dataset, tmp_path / 'atomic.parquet')
    before = path.read_bytes()

    def fail(*args, **kwargs):
        if stage == 'write':
            args[1].write(b'partial cache')
        raise OSError('injected I/O failure')

    monkeypatch.setattr(storage.pq if stage == 'write' else storage.os,
                        'write_table' if stage == 'write' else 'replace', fail)
    with pytest.raises(OSError, match='injected'):
        cache_dataset(sample_dataset, path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_volume_outside_storage_range_fails_before_replacing(sample_dataset, tmp_path):
    path = cache_dataset(sample_dataset, tmp_path / 'range.parquet')
    before = path.read_bytes()
    sample_dataset.bars[0] = replace(sample_dataset.bars[0], volume=2 ** 63)
    sample_dataset.version = dataset_version(sample_dataset)
    with pytest.raises(DataIntegrityError):
        cache_dataset(sample_dataset, path)
    assert path.read_bytes() == before


def test_empty_actions_and_false_synthetic_are_explicit(sample_dataset, tmp_path):
    sample_dataset.actions = []
    sample_dataset.synthetic = False
    sample_dataset.version = dataset_version(sample_dataset)
    path = cache_dataset(sample_dataset, tmp_path / 'no-actions.parquet')
    assert read_dataset(path) == sample_dataset
    payload = json.loads(pq.ParquetFile(path).schema_arrow.metadata[b'quantlab.dataset'])
    assert payload['actions'] == []
    assert payload['synthetic'] is False
