import json
from decimal import Decimal

import pytest

from quantlab.data import cache_dataset, generate_synthetic
from quantlab.engine import Platform
from quantlab.models import BacktestResult


@pytest.fixture
def fast_platform(tmp_path, monkeypatch):
    platform = Platform(tmp_path)
    calls = []

    def backtest(dataset, strategy, parameters, cost_multiplier=1):
        calls.append((strategy, parameters.copy(), cost_multiplier))
        return BacktestResult([], [], [], platform._account(), metrics={'total_return': 0}, synthetic=dataset.synthetic)

    monkeypatch.setattr(platform, '_backtest', backtest)
    monkeypatch.setattr(platform, '_benchmarks', lambda dataset: {})
    monkeypatch.setattr('quantlab.engine.validate_strategy', lambda *a, **kw: {
        'passed': False, 'verdict': 'NOT_VALIDATED_SYNTHETIC'})
    yield platform, calls
    platform.close()


def test_holdout_replay_is_immutable_no_reexecution_after_restart(fast_platform, monkeypatch):
    platform, calls = fast_platform
    first = platform.run_demo()
    assert first['execution']['fills'] > 0
    before = platform.snapshot()
    again = platform.run_demo()
    assert again['replayed_from_sealed_result'] is True
    assert again['run_id'] != first['run_id']
    assert again['execution']['fills'] == 0
    assert len(calls) == 1
    assert platform.snapshot()['fills'] == before['fills']
    restored = Platform(platform.root)
    try:
        monkeypatch.setattr(restored, '_backtest', lambda *a, **k: pytest.fail('sealed backtest reran'))
        assert restored.run_demo()['replayed_from_sealed_result'] is True
        assert restored.snapshot()['portfolio'] == before['portfolio']
    finally:
        restored.close()


def test_holdout_rejects_retuning_and_records_failure(fast_platform):
    platform, calls = fast_platform
    platform.run_demo()
    with pytest.raises(ValueError, match='Holdout sealed'):
        platform.run_demo(lookback=126)
    assert len(calls) == 1
    assert platform.registry.list_experiments()[-1]['status'] == 'failed'


def test_interrupted_research_stays_sealed(fast_platform, monkeypatch):
    platform, calls = fast_platform
    monkeypatch.setattr(platform, '_backtest', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('test failure')))
    with pytest.raises(RuntimeError, match='test failure'):
        platform.run_demo()
    with pytest.raises(ValueError, match='Holdout sealed'):
        platform.run_demo()
    assert all(row['status'] == 'failed' for row in platform.registry.list_experiments())
    assert platform.broker.orders() == []


def test_stop_blocks_new_orders_but_not_research_and_never_liquidates(fast_platform):
    platform, calls = fast_platform
    platform.emergency_halt('test stop')
    result = platform.run_demo()
    assert result['execution']['orders'] == 0
    assert result['execution']['rejections']
    assert platform.broker.orders() == []
    assert platform.broker.fills() == []
    assert platform.risk.status()['halted'] is True


def test_dataset_version_cannot_mix_simulated_accounts(fast_platform):
    platform, calls = fast_platform
    platform.run_demo()
    fills = platform.broker.fills()
    result = platform.run_demo(seed=8)
    assert 'different data version' in result['execution']['blocked']
    assert platform.broker.fills() == fills


def test_imported_parquet_never_executes_and_preserves_provenance(fast_platform, tmp_path):
    platform, calls = fast_platform
    dataset = generate_synthetic()
    path = cache_dataset(dataset, tmp_path / 'source.parquet')
    report = platform.research_file(path)
    assert report['synthetic'] is True
    assert report['execution']['mode'] == 'RESEARCH_ONLY'
    assert platform.broker.orders() == []
    assert platform.dataset.version == dataset.version
    with pytest.raises(ValueError, match='Holdout sealed'):
        platform.research_file(path, seed=8)


@pytest.mark.parametrize('parameters', [{'lookback': True}, {'seed': True}, {'lookback': 9}, {'strategy': 'other'}])
def test_public_parameters_fail_closed(fast_platform, parameters):
    platform, _ = fast_platform
    with pytest.raises(ValueError):
        platform.run_demo(**parameters)


def test_cancel_requires_separate_authorization(fast_platform):
    platform, _ = fast_platform
    with pytest.raises(PermissionError):
        platform.cancel_all()
    assert platform.cancel_all(authorized=True)['no_liquidation'] is True
