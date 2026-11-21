from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from quantlab.models import EquityPoint, jsonable
from quantlab.research import ExperimentRegistry
import quantlab.research.registry as registry_module


@pytest.fixture
def registry(tmp_path):
    return ExperimentRegistry(tmp_path / 'nested' / 'experiments.sqlite3')


def start(registry, **overrides):
    values = dict(data_version='data-sha256-v1', code_version='code-sha256-v1',
                  strategy='trend', parameters={'lookback': 63}, seed=7, synthetic=True)
    values.update(overrides)
    return registry.start(**values)


def test_lifecycle_provenance_utc_and_restart(registry):
    before = datetime.now(timezone.utc)
    parameters = {'lookback': 63, 'cap': Decimal('0.3500')}
    run_id = start(registry, parameters=parameters)
    parameters['lookback'] = 999
    row = registry.list_experiments()[0]
    assert row == {
        'run_id': run_id, 'data_version': 'data-sha256-v1', 'code_version': 'code-sha256-v1',
        'strategy': 'trend', 'parameters': {'lookback': 63, 'cap': '0.3500'}, 'seed': 7,
        'synthetic': True, 'status': 'running', 'started_at': row['started_at'],
        'finished_at': None, 'result': None, 'error': None,
    }
    restarted = ExperimentRegistry(registry.db_path)
    assert restarted.list_experiments() == [row]
    result = {'metrics': {'total_return': -0.2, 'sharpe': None}, 'warnings': ['short sample']}
    assert restarted.finish(run_id, result) is None
    result['metrics']['total_return'] = 1.0
    final = registry.list_experiments()[0]
    assert final['status'] == 'completed'
    assert final['result']['metrics']['total_return'] == -0.2
    timestamp_pattern = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00'
    for key in ('started_at', 'finished_at'):
        assert re.fullmatch(timestamp_pattern, final[key])
        parsed = datetime.fromisoformat(final[key])
        assert parsed.utcoffset() == timedelta(0)
        assert before <= parsed <= datetime.now(timezone.utc)
    assert final['finished_at'] >= final['started_at']
    assert ExperimentRegistry(registry.db_path).list_experiments() == [final]
    # A consumer mutating a returned dictionary must not rewrite provenance.
    final['parameters']['lookback'] = 0
    assert registry.list_experiments()[0]['parameters']['lookback'] == 63


def test_failures_negative_results_and_repeated_inputs_are_all_kept(registry):
    successful = start(registry)
    failed = start(registry)
    pending = start(registry)
    registry.finish(successful, {'return': -0.6})
    registry.finish(failed, {'partial_curve': [1, 0.4], 'metric': None},
                    status='failed', error='evaluation failed: missing bars')
    rows = ExperimentRegistry(registry.db_path).list_experiments()
    assert len({row['run_id'] for row in rows}) == 3
    assert [row['run_id'] for row in rows] == [successful, failed, pending]
    assert [row['status'] for row in rows] == ['completed', 'failed', 'running']
    assert rows[1]['error'] == 'evaluation failed: missing bars'
    assert rows[1]['result'] == {'partial_curve': [1, 0.4], 'metric': None}
    assert rows[1]['finished_at'] is not None
    assert rows[2]['finished_at'] is None


@pytest.mark.parametrize('status,error', [('completed', None), ('failed', 'bad data'), ('failed', None)])
def test_terminal_results_are_immutable_and_exact_retries_idempotent(registry, status, error):
    run_id = start(registry)
    registry.finish(run_id, {'z': 2, 'a': {'x': 1}}, status=status, error=error)
    original = registry.list_experiments()
    restarted = ExperimentRegistry(registry.db_path)
    restarted.finish(run_id, {'a': {'x': 1}, 'z': 2}, status=status, error=error)
    assert registry.list_experiments() == original
    with pytest.raises(ValueError, match='terminal'):
        registry.finish(run_id, {'z': 3}, status=status, error=error)
    other_status = 'failed' if status == 'completed' else 'completed'
    with pytest.raises(ValueError, match='terminal'):
        registry.finish(run_id, {'z': 2, 'a': {'x': 1}}, status=other_status)
    assert registry.list_experiments() == original


def test_error_cannot_be_edited_after_failure(registry):
    run_id = start(registry)
    registry.finish(run_id, {}, status='failed', error='first failure')
    with pytest.raises(ValueError, match='terminal'):
        registry.finish(run_id, {}, status='failed', error='more flattering wording')
    assert registry.list_experiments()[0]['error'] == 'first failure'


@pytest.mark.parametrize('status', ['running', 'success', 'cancelled', 'COMPLETED', '', None])
def test_invalid_finish_status_does_not_write(registry, status):
    run_id = start(registry)
    original = registry.list_experiments()
    with pytest.raises(ValueError, match='status'):
        registry.finish(run_id, {}, status=status)
    assert registry.list_experiments() == original


def test_unknown_run_and_inconsistent_error_do_not_write(registry):
    with pytest.raises(KeyError):
        registry.finish('unknown', {})
    run_id = start(registry)
    original = registry.list_experiments()
    with pytest.raises(ValueError, match='error'):
        registry.finish(run_id, {}, error='cannot call this completed')
    with pytest.raises(TypeError, match='error'):
        registry.finish(run_id, {}, status='failed', error=RuntimeError('bad'))
    assert registry.list_experiments() == original


class ResultKind(Enum):
    NEGATIVE = 'negative'


def test_json_matches_models_preserves_precision_and_is_canonical(registry):
    at = datetime(2026, 11, 21, 13, 2, 3, 123456, tzinfo=timezone.utc)
    point = EquityPoint(at=at, cash=Decimal('1234567890123456789.123400'),
                        equity=Decimal('1.0000'), gross_exposure=Decimal('0.3500'))
    payload = {'unicode': '失败结果', 'curve': (point,), 'session': date(2026, 11, 21),
               'kind': ResultKind.NEGATIVE, 'null': None, 'boolean': False, 'zero': 0.0}
    run_id = start(registry, parameters=payload, seed=2 ** 80, synthetic=False)
    registry.finish(run_id, payload)
    row = registry.list_experiments()[0]
    assert row['parameters'] == jsonable(payload)
    assert row['result'] == jsonable(payload)
    assert row['seed'] == 2 ** 80
    assert row['synthetic'] is False
    encoded = json.dumps(jsonable(payload), sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False)
    with sqlite3.connect(registry.db_path) as connection:
        raw = connection.execute('SELECT parameters, result FROM research_experiments').fetchone()
        assert raw == (encoded, encoded)
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    # Reused references are allowed; only actual cycles are forbidden.
    reused = {'a': [1]}
    registry.finish(start(registry), {'one': reused, 'two': reused})


@pytest.mark.parametrize('bad', [
    float('nan'), float('inf'), float('-inf'),
    Decimal('NaN'), Decimal('sNaN'), Decimal('Infinity'),
    datetime(2026, 1, 1),
    datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    {1: 'non-string key', '1': 'would collide'},
    {1, 2}, b'bytes', object(),
    EquityPoint(at=datetime(2026, 1, 1), cash=Decimal('1'),
                equity=Decimal('1'), gross_exposure=Decimal('0')),
])
def test_invalid_nested_json_rejected_before_any_write(registry, bad):
    with pytest.raises((ValueError, TypeError)):
        start(registry, parameters={'nested': [bad]})
    assert registry.list_experiments() == []
    run_id = start(registry)
    original = registry.list_experiments()
    with pytest.raises((ValueError, TypeError)):
        registry.finish(run_id, {'nested': [bad]})
    assert registry.list_experiments() == original
    registry.finish(run_id, {'recovered': True})


@pytest.mark.parametrize('container', ['list', 'dict'])
def test_cycles_are_rejected_cleanly(registry, container):
    value = [] if container == 'list' else {}
    if container == 'list':
        value.append(value)
    else:
        value['self'] = value
    with pytest.raises(ValueError, match='Circular'):
        start(registry, parameters={'value': value})
    assert registry.list_experiments() == []


@pytest.mark.parametrize('field,value', [
    ('data_version', ''), ('code_version', '  '), ('strategy', None),
    ('parameters', []), ('parameters', None), ('seed', True), ('seed', 7.0),
    ('seed', '7'), ('synthetic', 1), ('synthetic', 'false'),
])
def test_invalid_provenance_does_not_create_run(registry, field, value):
    with pytest.raises((TypeError, ValueError)):
        start(registry, **{field: value})
    assert registry.list_experiments() == []


@pytest.mark.parametrize('result', [None, [], 1, 'result'])
def test_result_must_be_object(registry, result):
    run_id = start(registry)
    with pytest.raises(TypeError, match='dictionary'):
        registry.finish(run_id, result)
    assert registry.list_experiments()[0]['status'] == 'running'


def test_sql_and_json_text_are_data_not_executable(registry):
    attack = "'); DROP TABLE research_experiments; --"
    payload = {'__class__': 'os.system', '__reduce__': attack, 'query': attack}
    run_id = start(registry, data_version=attack, code_version=attack,
                   strategy=attack, parameters=payload)
    registry.finish(run_id, payload, status='failed', error=attack)
    row = registry.list_experiments()[0]
    assert row['data_version'] == attack
    assert row['code_version'] == attack
    assert row['strategy'] == attack
    assert row['parameters'] == row['result'] == payload
    assert row['error'] == attack
    with pytest.raises(KeyError):
        registry.finish(attack, {})
    assert len(registry.list_experiments()) == 1
    start(registry)
    assert len(registry.list_experiments()) == 2


def test_write_failure_is_atomic_and_retryable(registry):
    run_id = start(registry)
    original = registry.list_experiments()
    with sqlite3.connect(registry.db_path) as connection:
        connection.execute('''CREATE TRIGGER reject_finish BEFORE UPDATE ON research_experiments
                              BEGIN SELECT RAISE(ABORT, 'test write failure'); END''')
    with pytest.raises(sqlite3.IntegrityError, match='test write failure'):
        registry.finish(run_id, {'complete': True})
    assert ExperimentRegistry(registry.db_path).list_experiments() == original
    with sqlite3.connect(registry.db_path) as connection:
        connection.execute('DROP TRIGGER reject_finish')
    registry.finish(run_id, {'complete': True})
    assert registry.list_experiments()[0]['status'] == 'completed'


def test_sqlite_constraints_reject_inconsistent_lifecycle(registry):
    start(registry)
    with sqlite3.connect(registry.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE research_experiments SET status = 'invented'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE research_experiments SET status = 'completed'")
    assert registry.list_experiments()[0]['status'] == 'running'


def test_clock_regression_does_not_fabricate_finish_time(registry, monkeypatch):
    monkeypatch.setattr(registry_module, '_utc_now', lambda: '2026-11-21T12:00:00.000000+00:00')
    run_id = start(registry)
    monkeypatch.setattr(registry_module, '_utc_now', lambda: '2026-11-21T11:59:59.000000+00:00')
    with pytest.raises(ValueError, match='clock moved backwards'):
        registry.finish(run_id, {})
    assert registry.list_experiments()[0]['finished_at'] is None
    monkeypatch.setattr(registry_module, '_utc_now', lambda: '2026-11-21T12:00:00.000001+00:00')
    registry.finish(run_id, {})


def test_concurrent_threads_preserve_all_runs(registry):
    def worker(index):
        run_id = start(registry, seed=index)
        status = 'failed' if index % 2 else 'completed'
        registry.finish(run_id, {'index': index}, status=status,
                        error='deliberate failure' if status == 'failed' else None)
        return run_id

    with ThreadPoolExecutor(max_workers=6) as pool:
        run_ids = list(pool.map(worker, range(18)))
    rows = ExperimentRegistry(registry.db_path).list_experiments()
    assert {row['run_id'] for row in rows} == set(run_ids)
    assert len(rows) == 18
    assert sum(row['status'] == 'failed' for row in rows) == 9
    assert {row['result']['index'] for row in rows} == set(range(18))
    assert [(row['started_at'], row['run_id']) for row in rows] == sorted(
        (row['started_at'], row['run_id']) for row in rows)


def test_competing_finishes_cannot_overwrite_each_other(registry):
    run_id = start(registry)
    barrier = Barrier(2)

    def worker(index):
        local = ExperimentRegistry(registry.db_path)
        barrier.wait(timeout=5)
        try:
            local.finish(run_id, {'winner': index})
            return 'committed', index
        except ValueError:
            return 'rejected', index

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, (1, 2)))
    assert sorted(outcome[0] for outcome in outcomes) == ['committed', 'rejected']
    winner = next(index for status, index in outcomes if status == 'committed')
    assert registry.list_experiments()[0]['result'] == {'winner': winner}


def test_process_exit_retains_committed_start_and_rolls_back_uncommitted_finish(registry):
    script = '''
import os
import sqlite3
import sys
from quantlab.research import ExperimentRegistry
registry = ExperimentRegistry(sys.argv[1])
run_id = registry.start('data-v1', 'code-v1', 'momentum', {'lookback': 63}, 7, True)
connection = sqlite3.connect(sys.argv[1])
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE research_experiments SET status='completed', result='{}', "
                   "finished_at='9999-12-31T00:00:00.000000+00:00' WHERE run_id=?", (run_id,))
os._exit(23)
'''
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE='1')
    process = subprocess.run([sys.executable, '-c', script, str(registry.db_path)],
                             capture_output=True, text=True, timeout=15, env=environment)
    assert process.returncode == 23, process.stderr
    restarted = ExperimentRegistry(registry.db_path)
    rows = restarted.list_experiments()
    assert len(rows) == 1
    assert rows[0]['status'] == 'running'
    assert rows[0]['result'] is None
    assert rows[0]['finished_at'] is None
    restarted.finish(rows[0]['run_id'], {'partial': True}, status='failed', error='interrupted')
    assert registry.list_experiments()[0]['error'] == 'interrupted'
    with sqlite3.connect(registry.db_path) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_empty_registry_and_memory_database_rejection(registry):
    assert registry.list_experiments() == []
    with pytest.raises(ValueError, match='persistent'):
        ExperimentRegistry(':memory:')
