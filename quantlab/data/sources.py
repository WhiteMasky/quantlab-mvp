"""Replaceable local-only adapters and an intentionally strict CSV contract.

CSV required header (any order):
    symbol,session,open_at,close_at,available_at,open,high,low,close,adjusted_close,volume
Optional action columns, all four together:
    ex_date,known_at,split_ratio,cash_dividend

UTF-8 (optional BOM), comma-delimited, one complete daily bar per symbol/session.
No inferred dates, prices, timezone, availability, adjustments, or actions.
Dates: YYYY-MM-DD. UTC timestamps: YYYY-MM-DDTHH:MM:SS[.ffffff]Z or +00:00.
Prices: plain unsigned decimal notation; OHLC are RAW, adjusted_close is a
separate mandatory research-only field. Volume: whole nonnegative shares.
Symbols: canonical uppercase US equity/ETF symbols (BRK.B and BRK-B supported).
Whitespace, duplicate/unknown headers, blank required cells, uneven rows,
non-finite values and duplicate bars/actions are rejected, never coerced.
An action row needs all four values; all four blank means no action. ex_date
must match that row's session; split_ratio is new shares / old shares and
cash_dividend is USD per post-split share. Combine same-day split/dividend in
one row, using 1 and 0 explicitly when applicable. known_at is mandatory UTC.
The loader cannot verify whether a vendor secretly pre-adjusted its raw OHLC.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

from quantlab.models import CorporateAction, Dataset, MarketBar
from .integrity import DATA_LIMITATIONS, DataIntegrityError, _valid_symbol, dataset_version, validate_dataset
from .synthetic import generate_synthetic

CSV_BAR_FIELDS = (
    'symbol', 'session', 'open_at', 'close_at', 'available_at',
    'open', 'high', 'low', 'close', 'adjusted_close', 'volume',
)
CSV_ACTION_FIELDS = ('ex_date', 'known_at', 'split_ratio', 'cash_dividend')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z', re.ASCII)
_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z', re.ASCII)
_DECIMAL = re.compile(r'(?:0|[1-9]\d*)(?:\.\d+)?\Z', re.ASCII)
_INTEGER = re.compile(r'(?:0|[1-9]\d*)\Z', re.ASCII)


@runtime_checkable
class DataSource(Protocol):
    """Swap CSV, Parquet or synthetic adapters without network side effects."""

    def load(self, path: str | Path | None = None) -> Dataset:
        ...


def _date(text):
    if not _DATE.fullmatch(text):
        raise ValueError('Expected an exact YYYY-MM-DD exchange date')
    return date.fromisoformat(text)


def _timestamp(text):
    if not _TIMESTAMP.fullmatch(text):
        raise ValueError('Timestamp must be explicit ISO-8601 UTC with seconds')
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _decimal(text):
    if not _DECIMAL.fullmatch(text):
        raise ValueError('Expected a plain unsigned decimal, without exponent/NaN/whitespace')
    return Decimal(text)


def _failure(message, row=None, dataset=None, code='CSV_FORMAT'):
    issue = {'severity': 'error', 'code': code, 'message': message}
    if row is not None:
        issue['row'] = row
    return DataIntegrityError(message, [issue], dataset)


class CSVDataSource:
    """Load the module's strict schema; structural/semantic errors fail closed.

    Missing sessions and suspect-but-possible moves are retained and reported
    by validate_dataset rather than filled or silently discarded. A semantic
    DataIntegrityError retains the full parsed dataset and all diagnostics.
    CSV provenance/actual publication times are unverified. ``synthetic=True``
    must be set explicitly when importing external synthetic fixtures.
    """

    def __init__(self, *, synthetic: bool = False, source: str = 'CSV local (unverified)'):
        if type(synthetic) is not bool or not isinstance(source, str) or not source.strip():
            raise ValueError('source must be nonempty and synthetic must be boolean')
        self.synthetic = synthetic
        self.source = source

    def load(self, path: str | Path | None = None) -> Dataset:
        if path is None:
            raise ValueError('CSV path is required')
        dataset = Dataset('', self.source, self.synthetic, [], limitations=[
            *DATA_LIMITATIONS,
            'Local CSV supplied by caller; raw-versus-adjusted semantics and corporate-action '
            'completeness are asserted, not verified. No external data was downloaded.',
        ])
        if self.synthetic:
            dataset.limitations.append('SYNTHETIC CSV: software-test data, not investment evidence.')
        try:
            with Path(path).open('r', encoding='utf-8-sig', newline='') as handle:
                reader = csv.reader(handle, strict=True)
                header = next(reader, None)
                if not header or len(header) != len(set(header)):
                    raise _failure('Missing or duplicate CSV headers', 1, dataset)
                required, optional = set(CSV_BAR_FIELDS), set(CSV_ACTION_FIELDS)
                fields = set(header)
                if not required <= fields or fields - required - optional:
                    raise _failure('CSV requires exactly the documented raw/adjusted bar fields and optional action fields', 1, dataset)
                action_fields = fields & optional
                if action_fields and action_fields != optional:
                    raise _failure('Corporate-action columns must be provided as a complete group', 1, dataset)
                for values in reader:
                    line = reader.line_num
                    if len(values) != len(header):
                        raise _failure('CSV row width does not match its header', line, dataset)
                    row = dict(zip(header, values))
                    try:
                        if not _valid_symbol(row['symbol']):
                            raise ValueError('Invalid canonical uppercase symbol')
                        if not _INTEGER.fullmatch(row['volume']):
                            raise ValueError('volume must be an unsigned whole-share integer')
                        session = _date(row['session'])
                        bar = MarketBar(
                            symbol=row['symbol'], session=session,
                            open_at=_timestamp(row['open_at']), close_at=_timestamp(row['close_at']),
                            available_at=_timestamp(row['available_at']),
                            open=_decimal(row['open']), high=_decimal(row['high']), low=_decimal(row['low']),
                            close=_decimal(row['close']), adjusted_close=_decimal(row['adjusted_close']),
                            volume=int(row['volume']),
                        )
                        action = None
                        if action_fields and any(row[field] for field in CSV_ACTION_FIELDS):
                            if not all(row[field] for field in CSV_ACTION_FIELDS):
                                raise ValueError('An action needs all four nonempty action values')
                            ex_date = _date(row['ex_date'])
                            if ex_date != session:
                                raise ValueError('Embedded action ex_date must match the row session')
                            action = CorporateAction(
                                bar.symbol, ex_date, _timestamp(row['known_at']),
                                _decimal(row['split_ratio']), _decimal(row['cash_dividend']),
                            )
                        dataset.bars.append(bar)
                        if action is not None:
                            dataset.actions.append(action)
                    except (ValueError, ArithmeticError) as exc:
                        raise _failure(f'CSV row {line}: {exc}', line, dataset) from exc
        except (csv.Error, UnicodeError) as exc:
            raise _failure(f'Invalid UTF-8 CSV: {exc}', dataset=dataset) from exc
        dataset.version = dataset_version(dataset)
        issues = validate_dataset(dataset)
        if any(issue['severity'] == 'error' for issue in issues):
            raise DataIntegrityError('CSV contains invalid data; nothing was repaired or dropped', issues, dataset)
        return dataset


class SyntheticDataSource:
    """Adapter for explicitly labeled deterministic synthetic fixtures."""

    def __init__(self, seed=7, sessions=504, symbols=('SPY', 'QQQ', 'IWM', 'TLT')):
        self.seed, self.sessions, self.symbols = seed, sessions, tuple(symbols) if not isinstance(symbols, str) else symbols

    def load(self, path: str | Path | None = None) -> Dataset:
        if path is not None:
            raise ValueError('SyntheticDataSource does not read a path')
        return generate_synthetic(self.seed, self.sessions, self.symbols)


class ParquetDataSource:
    """Read a verified QuantLab cache, not arbitrary provider Parquet files."""

    def load(self, path: str | Path | None = None) -> Dataset:
        from .storage import read_dataset

        if path is None:
            raise ValueError('Parquet path is required')
        return read_dataset(path)
