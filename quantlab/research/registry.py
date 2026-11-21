"""Durable, unranked experiment records; no optimizer or execution side effects.

Rows are returned as JSON-compatible dictionaries, ordered by UTC start time and
run ID. Timestamps use ISO 8601 with six fractional digits and ``+00:00``.
Unfinished runs survive restart as ``running``: callers must explicitly finish
or mark them ``failed``; a restart does not fabricate a successful result.

Parameters/results are JSON objects. Nested dataclasses, tuples, finite Decimal
values, dates and UTC-aware datetimes follow ``models.jsonable`` conventions.
Decimals become strings without rounding. Non-string object keys, non-finite
numbers, non-UTC datetimes, cycles and unsupported objects are rejected before a
write. Timestamp-like strings remain opaque strings, not inferred timestamps.
The stored JSON has sorted keys, compact separators and no NaN/Infinity tokens.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from quantlab.models import jsonable, require_utc


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_experiments (
    run_id TEXT PRIMARY KEY NOT NULL,
    data_version TEXT NOT NULL,
    code_version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    parameters TEXT NOT NULL,
    seed TEXT NOT NULL,
    synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1)),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    result TEXT,
    error TEXT,
    CHECK (
        (status = 'running' AND finished_at IS NULL AND result IS NULL AND error IS NULL)
        OR (status = 'completed' AND finished_at IS NOT NULL AND result IS NOT NULL AND error IS NULL)
        OR (status = 'failed' AND finished_at IS NOT NULL AND result IS NOT NULL)
    ),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
)
"""


def _json_value(value: Any, ancestors: set[int]) -> Any:
    """Validate before conversion so coercion cannot hide invalid values."""
    if isinstance(value, Enum):
        return _json_value(value.value, ancestors)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('JSON numbers must be finite')
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Decimal values must be finite')
        return jsonable(value)
    if isinstance(value, datetime):
        require_utc(value)
        return jsonable(value)
    if isinstance(value, date):
        return jsonable(value)

    dataclass_instance = is_dataclass(value) and not isinstance(value, type)
    if not dataclass_instance and not isinstance(value, (dict, list, tuple)):
        raise TypeError(f'Unsupported JSON value: {type(value).__name__}')
    identity = id(value)
    if identity in ancestors:
        raise ValueError('Circular JSON value')
    ancestors.add(identity)
    try:
        if dataclass_instance:
            return {field.name: _json_value(getattr(value, field.name), ancestors)
                    for field in fields(value)}
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise TypeError('JSON object keys must be strings')
            return {key: _json_value(item, ancestors) for key, item in value.items()}
        return [_json_value(item, ancestors) for item in value]
    finally:
        ancestors.remove(identity)


def _encode_object(value: dict, name: str) -> str:
    if not isinstance(value, dict):
        raise TypeError(f'{name} must be a dictionary')
    return json.dumps(_json_value(value, set()), sort_keys=True,
                      separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _nonempty_string(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a nonempty string')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class ExperimentRegistry:
    """SQLite registry with atomic, immutable terminal results.

    ``start`` always creates a new run, even for identical inputs; failed or poor
    results are never deduplicated or filtered. Legal transitions are only
    ``running -> completed|failed``. Repeating an identical finish is harmless;
    changing a terminal result raises ValueError. Unknown IDs raise KeyError.
    A failed run may have an optional error and partial result; completed runs
    cannot carry an error. There is deliberately no delete, rank or retry API.

    File-backed SQLite is required. Connections are short-lived and local to
    each call (safe across service threads). WAL, FULL synchronous commits and
    a ten-second busy timeout protect normal concurrent/restart use. Hardware
    durability still depends on the filesystem; this is not a tamper-proof log
    or a guarantee of exactly-once strategy evaluation.
    """

    def __init__(self, db_path):
        if str(db_path) == ':memory:':
            raise ValueError('ExperimentRegistry requires a persistent database path')
        self.db_path = Path(db_path).expanduser().absolute()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute(_SCHEMA)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA synchronous=FULL')
            with connection:
                yield connection
        finally:
            connection.close()

    def start(self, data_version, code_version, strategy, parameters, seed, synthetic) -> str:
        """Commit provenance before evaluation; versions are caller-supplied."""
        for name, value in (('data_version', data_version), ('code_version', code_version),
                            ('strategy', strategy)):
            _nonempty_string(value, name)
        if type(seed) is not int:
            raise TypeError('seed must be an integer, not a boolean')
        if type(synthetic) is not bool:
            raise TypeError('synthetic must be a boolean')
        encoded_parameters = _encode_object(parameters, 'parameters')
        run_id = str(uuid4())
        # Store the seed as decimal text to preserve integers beyond SQLite int64.
        with self._connection() as connection:
            connection.execute(
                'INSERT INTO research_experiments '
                '(run_id, data_version, code_version, strategy, parameters, seed, synthetic, '
                'started_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (run_id, data_version, code_version, strategy, encoded_parameters,
                 str(seed), int(synthetic), _utc_now(), 'running'),
            )
        return run_id

    def finish(self, run_id, result: dict, status='completed', error=None):
        """Atomically seal a run; preserve earlier results on any invalid write."""
        _nonempty_string(run_id, 'run_id')
        if status not in ('completed', 'failed'):
            raise ValueError('finish status must be completed or failed')
        if error is not None and not isinstance(error, str):
            raise TypeError('error must be a string or None')
        if status == 'completed' and error is not None:
            raise ValueError('A completed run cannot carry an error')
        encoded_result = _encode_object(result, 'result')
        with self._connection() as connection:
            # Lock before reading: competing finishes cannot overwrite each other.
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT * FROM research_experiments WHERE run_id = ?', (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row['status'] != 'running':
                if (row['status'], row['result'], row['error']) == (status, encoded_result, error):
                    return
                raise ValueError('A terminal experiment cannot be changed')
            finished_at = _utc_now()
            if finished_at < row['started_at']:
                raise ValueError('UTC clock moved backwards; experiment remains running')
            connection.execute(
                'UPDATE research_experiments SET result = ?, status = ?, error = ?, '
                'finished_at = ? WHERE run_id = ?',
                (encoded_result, status, error, finished_at, run_id),
            )

    def list_experiments(self) -> list[dict]:
        """Return every run, including running/failed ones, without score ranking."""
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT * FROM research_experiments ORDER BY started_at, run_id',
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record['parameters'] = json.loads(record['parameters'])
            record['result'] = json.loads(record['result']) if record['result'] is not None else None
            record['seed'] = int(record['seed'])
            record['synthetic'] = bool(record['synthetic'])
            records.append(record)
        return records
