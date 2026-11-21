"""Offline daily market data with explicit provenance and availability."""
from .calendar import session_times
from .integrity import DataIntegrityError, dataset_version, validate_dataset
from .sources import CSVDataSource, DataSource, ParquetDataSource, SyntheticDataSource
from .storage import cache_dataset, read_dataset
from .synthetic import generate_synthetic

__all__ = [
    'CSVDataSource',
    'DataIntegrityError',
    'DataSource',
    'ParquetDataSource',
    'SyntheticDataSource',
    'cache_dataset',
    'dataset_version',
    'generate_synthetic',
    'read_dataset',
    'session_times',
    'validate_dataset',
]
