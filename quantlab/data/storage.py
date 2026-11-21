"""Single-file, content-checked Parquet caches for the shared Dataset model.

Prices are separate lossless Decimal-text columns, never floats. Dates and UTC
publication times are typed Parquet columns. Dataset metadata and actions live
in the same file's schema metadata, so copying the file needs no sidecar. These
are QuantLab caches, not a generic vendor-Parquet importer or an authenticity
signature. Availability is preserved, never inferred or advanced on reload.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from quantlab.models import CorporateAction, Dataset, MarketBar, jsonable
from .integrity import DataIntegrityError, dataset_version, validate_dataset

_FORMAT = 'quantlab-parquet-v1'
_METADATA_KEY = b'quantlab.dataset'
_PRICES = ('open', 'high', 'low', 'close', 'adjusted_close')
_BAR_SCHEMA = pa.schema([
    pa.field('symbol', pa.string(), nullable=False),
    pa.field('session', pa.date32(), nullable=False),
    *(pa.field(name, pa.timestamp('us', tz='UTC'), nullable=False)
      for name in ('open_at', 'close_at', 'available_at')),
    *(pa.field(name, pa.string(), nullable=False) for name in _PRICES),
    pa.field('volume', pa.int64(), nullable=False),
])
_ACTION_FIELDS = {'symbol', 'ex_date', 'known_at', 'split_ratio', 'cash_dividend'}
_METADATA_FIELDS = {'format', 'content_hash', 'version', 'source', 'synthetic', 'limitations', 'actions'}


def _failure(message: str, code='INVALID_CACHE', dataset=None) -> DataIntegrityError:
    return DataIntegrityError(message, [{'severity': 'error', 'code': code, 'message': message}], dataset)


def _validate(dataset: Dataset) -> None:
    issues = validate_dataset(dataset)
    if any(issue['severity'] == 'error' for issue in issues):
        raise DataIntegrityError('Invalid dataset; cache was not accepted or repaired', issues, dataset)


def cache_dataset(dataset: Dataset, path: str | Path) -> Path:
    """Validate and atomically replace a local Parquet file; return its path.

    The input, row ordering and version are unchanged. Non-content-hash vendor
    version labels are preserved too, with an independent semantic hash in the
    cache. Structural errors and stale content hashes fail closed; diagnostic
    warnings (including future availability) do not discard observations.
    Parquet volumes use signed 64-bit integers; prices retain arbitrary Decimal
    precision and scale. A failed write leaves any previous cache untouched.
    """
    _validate(dataset)
    path = Path(path)
    metadata = {
        'format': _FORMAT,
        'content_hash': dataset_version(dataset),
        'version': dataset.version,
        'source': dataset.source,
        'synthetic': dataset.synthetic,
        'limitations': dataset.limitations,
        'actions': jsonable(dataset.actions),
    }
    rows = []
    for bar in dataset.bars:
        row = {name: getattr(bar, name) for name in _BAR_SCHEMA.names}
        row.update({name: str(getattr(bar, name)) for name in _PRICES})
        rows.append(row)
    try:
        table = pa.Table.from_pylist(rows, schema=_BAR_SCHEMA).replace_schema_metadata({
            _METADATA_KEY: json.dumps(metadata, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode('utf-8'),
        })
    except (pa.ArrowException, ValueError, TypeError, OverflowError) as exc:
        raise _failure(f'Dataset cannot be encoded as QuantLab Parquet: {exc}', dataset=dataset) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent,
                                         prefix=f'.{path.name}.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            pq.write_table(table, handle, compression='zstd', version='2.6')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _metadata(table: pa.Table) -> dict:
    encoded = (table.schema.metadata or {}).get(_METADATA_KEY)
    if encoded is None:
        raise ValueError('Missing QuantLab dataset metadata')
    metadata = json.loads(encoded.decode('utf-8'))
    if not isinstance(metadata, dict) or set(metadata) != _METADATA_FIELDS:
        raise ValueError('Invalid QuantLab metadata fields')
    if metadata['format'] != _FORMAT:
        raise ValueError('Unsupported QuantLab cache format')
    if any(not isinstance(metadata[key], str) or not metadata[key].strip()
           for key in ('content_hash', 'version', 'source')):
        raise ValueError('Cache hash, version and source must be nonempty strings')
    if type(metadata['synthetic']) is not bool:
        raise ValueError('synthetic must be a boolean, never inferred')
    if not isinstance(metadata['limitations'], list) or any(
            not isinstance(item, str) for item in metadata['limitations']):
        raise ValueError('limitations must be a list of strings')
    if not isinstance(metadata['actions'], list):
        raise ValueError('actions must be an explicit list, even when empty')
    for action in metadata['actions']:
        if (not isinstance(action, dict) or set(action) != _ACTION_FIELDS
                or any(not isinstance(value, str) for value in action.values())):
            raise ValueError('Corporate-action fields must be explicit strings')
    return metadata


def read_dataset(path: str | Path) -> Dataset:
    """Read only this cache format, check its hash and validate all observations.

    Missing/invalid metadata, mismatched schemas, nulls, content changes and
    semantic errors are rejected rather than repaired. As-of filtering remains
    the caller's responsibility; future known_at/available_at values survive.
    """
    try:
        # ParquetFile avoids directory/partition discovery and unintended columns.
        with pq.ParquetFile(Path(path)) as parquet:
            table = parquet.read()
        if not table.schema.equals(_BAR_SCHEMA, check_metadata=False):
            raise ValueError('Unexpected QuantLab bar schema')
        if any(column.null_count for column in table.columns):
            raise ValueError('Null bar values are not permitted')
        metadata = _metadata(table)
        bars = []
        for row in table.to_pylist():
            row.update({name: Decimal(row[name]) for name in _PRICES})
            bars.append(MarketBar(**row))
        actions = [CorporateAction(
            symbol=action['symbol'], ex_date=date.fromisoformat(action['ex_date']),
            known_at=datetime.fromisoformat(action['known_at']),
            split_ratio=Decimal(action['split_ratio']), cash_dividend=Decimal(action['cash_dividend']),
        ) for action in metadata['actions']]
        dataset = Dataset(metadata['version'], metadata['source'], metadata['synthetic'],
                          bars, actions, metadata['limitations'])
        if dataset_version(dataset) != metadata['content_hash']:
            raise _failure('Cache content no longer matches its semantic hash', 'CACHE_HASH_MISMATCH', dataset)
        _validate(dataset)
        return dataset
    except DataIntegrityError:
        raise
    except (pa.ArrowException, ValueError, TypeError, ArithmeticError, UnicodeError) as exc:
        raise _failure(f'Invalid QuantLab Parquet cache: {exc}') from exc
