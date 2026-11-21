"""Protocol-only tests using scripted results, never a real or profitable strategy."""
import json
import math
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

import quantlab.validation as validation
from quantlab.models import CorporateAction
from quantlab.validation import chronological_splits, validate_strategy


def records(report, role):
    return [trial for trial in report['trials'] if trial['role'] == role]


def trial_by_id(report, trial_id):
    return next(trial for trial in report['trials'] if trial['trial_id'] == trial_id)


def test_public_contract_exports():
    assert set(validation.__all__) == {'performance_metrics', 'validate_strategy', 'chronological_splits'}
    assert all(callable(getattr(validation, name)) for name in validation.__all__)


def test_three_candidates_use_eleven_calls_validation_selection_and_one_last_holdout(
        dataset, grid, evaluator_factory):
    rates = {
        ('train', 2, 1.0): '0.05', ('train', 3, 1.0): '-0.01',
        ('validation', 2, 1.0): '0.002', ('validation', 3, 1.0): '0.004',
        ('validation', 4, 1.0): '0.003',
    }
    evaluator = evaluator_factory(rates=rates)
    report = validate_strategy(dataset, evaluator, grid)

    assert len(evaluator.calls) == report['evaluation_count'] == 11
    assert report['search_count'] == 3
    assert report['registration']['maximum_primary_evaluations'] == 11
    assert report['registration']['parameter_grid'] == grid
    assert report['registration']['selection_metric'] == 'validation.total_return'
    assert report['registration']['warmup_sessions'] == 5
    assert report['selected_candidate'] == 1
    assert report['selected_parameters'] == grid[1]
    assert report['selection_locked'] is True
    assert records(report, 'train')[1]['metrics']['total_return'] < 0
    assert report['failure_reasons'] == []  # training profits never screen or select
    assert [call[0].role for call in evaluator.calls] == [
        'train', 'train', 'train', 'validation', 'validation', 'validation',
        'rolling_1', 'rolling_2', 'cost_stress', 'cost_stress', 'test',
    ]
    for subset, parameters, cost in evaluator.calls[6:]:
        assert parameters == grid[1]
        assert subset.evaluation_sessions[0] > subset.warmup_sessions[-1]
    assert report['test_evaluations'] == 1
    assert report['test_trial_id'] == report['trials'][-1]['trial_id']
    assert report['test_metrics'] == report['trials'][-1]['metrics']
    assert report['passed'] is False
    assert report['verdict'] == 'NOT_VALIDATED_SYNTHETIC'
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize('count', [1, 2, 3])
def test_budget_counts_actual_calls_and_single_candidate_cannot_claim_sensitivity(
        dataset, grid, evaluator_factory, count):
    evaluator = evaluator_factory()
    report = validate_strategy(dataset, evaluator, grid[:count])
    assert report['evaluation_count'] == len(evaluator.calls) == 2 * count + 5
    assert report['test_evaluations'] == 1
    assert len(report['parameter_sensitivity']['trials']) == count
    if count == 1:
        assert 'not identifiable' in report['parameter_sensitivity']['explanation']
        assert any('at least two' in reason for reason in report['failure_reasons'])
    else:
        assert report['parameter_sensitivity']['explanation'] is None


def test_validation_ties_use_registration_order_not_training_or_holdout(dataset, grid, evaluator_factory):
    grid.reverse()
    evaluator = evaluator_factory(rates={('train', 2, 1.0): '0.03', 'test': '-0.005'})
    report = validate_strategy(dataset, evaluator, grid)
    assert report['selected_candidate'] == 0
    assert report['selected_parameters'] == grid[0]
    assert report['test_evaluations'] == 1
    assert evaluator.calls[-1][1] == grid[0]
    assert any('Nonpositive test' in reason for reason in report['failure_reasons'])


def test_cost_stress_and_sensitivity_reference_real_validation_trials(dataset, grid, evaluator_factory):
    evaluator = evaluator_factory(rates={('validation', 3, 1.0): '0.004', 'cost_stress': '0.004'})
    report = validate_strategy(dataset, evaluator, grid)
    winner = trial_by_id(report, report['selection_trial_id'])
    costs = [trial_by_id(report, trial_id) for trial_id in report['cost_stress']]
    assert costs[0] is winner
    assert [trial['cost_multiplier'] for trial in costs] == [1.0, 2.0, 3.0]
    assert [trial['role'] for trial in costs] == ['validation', 'cost_stress', 'cost_stress']
    assert all(trial['period'] == report['splits']['validation'] for trial in costs)
    assert all(trial['parameters'] == report['selected_parameters'] for trial in costs)
    assert costs[0]['metrics']['total_return'] > costs[1]['metrics']['total_return'] > costs[2]['metrics']['total_return']
    for trial in costs:
        cost = trial['cost_multiplier']
        sessions = trial['period']['evaluation_sample_size']
        # These values come from the fake's explicit curve and commission, not a metric proxy.
        expected = (1000 * 1.004 - cost) * 1.004 ** (sessions - 1) / 1000 - 1
        assert trial['metrics']['total_return'] == pytest.approx(expected)
        assert trial['metrics']['fee_ratio'] == pytest.approx(cost / 1000)
        assert trial['result']['fills'][-1]['commission'] == str(Decimal(str(cost)))
        assert trial['metrics']['total_return'] != trial['result']['metrics']['total_return']
    sensitivity = report['parameter_sensitivity']
    assert 'without additional evaluations' in sensitivity['method']
    assert [row['trial_id'] for row in sensitivity['trials']] == [row['trial_id'] for row in records(report, 'validation')]
    for row in sensitivity['trials']:
        trial = trial_by_id(report, row['trial_id'])
        assert row == {'trial_id': trial['trial_id'], 'parameters': trial['parameters'],
                       'total_return': trial['metrics']['total_return'], 'status': trial['status']}
    assert report['rolling_oos'] == [row['trial_id'] for row in report['trials'] if row['role'].startswith('rolling_')]
    assert [(call[0].role, call[2]) for call in evaluator.calls if call[2] != 1] == [
        ('cost_stress', 2.0), ('cost_stress', 3.0),
    ]


def test_warmup_gains_losses_and_fills_are_excluded_from_every_score(dataset, grid, evaluator_factory):
    gain = validate_strategy(dataset, evaluator_factory(warmup_start='1'), grid)
    loss = validate_strategy(dataset, evaluator_factory(warmup_start='1000000'), grid)
    for left, right in zip(gain['trials'], loss['trials']):
        assert left['status'] == right['status'] == 'completed'
        assert left['metrics'] == right['metrics']
        count = left['period']['evaluation_sample_size']
        cost = left['cost_multiplier']
        expected = (1000 * 1.002 - cost) * 1.002 ** (count - 1) / 1000 - 1
        assert left['metrics']['sample_size'] == count
        assert left['metrics']['total_return'] == pytest.approx(expected)
        assert left['metrics']['fee_ratio'] == pytest.approx(cost / 1000)
        scored_equity = [(1000 * 1.002 - cost) * 1.002 ** index for index in range(count)]
        assert left['metrics']['turnover'] == pytest.approx(20 / (sum(scored_equity) / count))
        assert len(left['result']['equity_curve']) == count + left['period']['warmup_sample_size']
        assert len(left['result']['fills']) == 2  # retained result, not destructively trimmed
    assert gain['test_metrics'] == loss['test_metrics']


@pytest.mark.parametrize(('data_flag', 'result_flag'), [(True, True), (True, False), (False, True)])
def test_synthetic_origin_cannot_pass_even_if_all_screen_conditions_are_positive(
        dataset, grid, evaluator_factory, data_flag, result_flag):
    dataset.synthetic = data_flag  # flag plumbing only: the fixture remains invented data
    evaluator = evaluator_factory(result_synthetic=result_flag)
    report = validate_strategy(dataset, evaluator, grid)
    assert report['failure_reasons'] == []
    assert report['uncertainty']['mean_daily_return_95ci'][0] > 0
    assert report['synthetic'] is True
    assert report['passed'] is False
    assert report['verdict'] == 'NOT_VALIDATED_SYNTHETIC'


@pytest.mark.parametrize('role', ['validation', 'rolling_1', 'rolling_2', 'cost_stress', 'test'])
@pytest.mark.parametrize('rate', ['0', '-0.001'])
def test_nonpositive_validation_oos_costs_or_holdout_are_rejected_without_retuning(
        dataset, grid, evaluator_factory, role, rate):
    dataset.synthetic = False  # isolate the return gate instead of the synthetic veto
    evaluator = evaluator_factory(rates={role: rate}, result_synthetic=False)
    report = validate_strategy(dataset, evaluator, grid)
    assert report['synthetic'] is False
    assert report['passed'] is False
    assert report['verdict'] == 'FAILED_RESEARCH_SCREEN'
    assert any(f'Nonpositive {role} return' in reason for reason in report['failure_reasons'])
    assert report['test_evaluations'] == 1
    assert len(evaluator.calls) == 11
    assert evaluator.calls[-1][0].role == 'test'
    assert all(call[1] == report['selected_parameters'] for call in evaluator.calls[6:])


def test_exact_zero_holdout_return_is_rejected(dataset, grid, evaluator_factory):
    dataset.synthetic = False

    def flat_test(result, subset, parameters, cost):
        if subset.role == 'test':
            result.equity_curve = [replace(point, cash=Decimal('1000'), equity=Decimal('1000'))
                                   for point in result.equity_curve]
        return result

    report = validate_strategy(dataset, evaluator_factory(transform=flat_test, result_synthetic=False), grid)
    assert report['test_metrics']['total_return'] == 0
    assert report['passed'] is False
    assert any('Nonpositive test return' in reason for reason in report['failure_reasons'])


@pytest.mark.parametrize('failed_key', [
    ('train', 2, 1.0), ('validation', 2, 1.0), ('rolling_1', 2, 1.0),
    ('rolling_2', 2, 1.0), ('cost_stress', 2, 2.0), ('cost_stress', 2, 3.0), ('test', 2, 1.0),
])
def test_failed_calls_are_retained_counted_and_never_retried(dataset, grid, evaluator_factory, failed_key):
    evaluator = evaluator_factory(failures=[failed_key])
    report = validate_strategy(dataset, evaluator, grid)
    failed = [trial for trial in report['trials'] if trial['status'] == 'failed']
    assert len(failed) == 1
    trial = failed[0]
    assert trial['role'] == failed_key[0]
    assert trial['metrics'] is None
    assert trial['result'] is None
    assert 'RuntimeError: scripted failure' in trial['error']
    assert any(f"Trial {trial['trial_id']}" in reason for reason in report['failure_reasons'])
    assert len(evaluator.calls) == report['evaluation_count'] == 11
    assert sum((call[0].role, call[1]['lookback'], call[2]) == failed_key for call in evaluator.calls) == 1
    assert report['test_evaluations'] == 1
    assert report['passed'] is False
    if failed_key[0] == 'validation':
        assert report['selected_candidate'] == 1
        assert report['parameter_sensitivity']['trials'][0]['status'] == 'failed'
        assert report['parameter_sensitivity']['trials'][0]['total_return'] is None
    if failed_key[0] == 'test':
        assert report['test_metrics'] is None
        assert report['uncertainty']['replications'] == 0


def test_no_eligible_validation_candidate_keeps_sealed_test_unopened(dataset, grid, evaluator_factory):
    evaluator = evaluator_factory(failures=['validation'])
    report = validate_strategy(dataset, evaluator, grid)
    assert report['evaluation_count'] == len(evaluator.calls) == 6
    assert report['test_evaluations'] == 0
    assert report['selection_locked'] is False
    assert report['selected_parameters'] is None
    assert report['cost_stress'] == report['rolling_oos'] == []
    assert len(report['parameter_sensitivity']['trials']) == 3
    assert all(trial['status'] == 'failed' for trial in records(report, 'validation'))
    assert any('sealed test was not accessed' in reason for reason in report['failure_reasons'])


@pytest.mark.parametrize('proxy', [{'total_return': 1}, 0.25, None])
def test_evaluator_must_return_full_backtest_result_not_metric_proxy(dataset, grid, proxy):
    calls = []

    def evaluator(subset, parameters, cost):
        calls.append(subset.role)
        return proxy

    report = validate_strategy(dataset, evaluator, grid)
    assert len(calls) == 6
    assert report['test_evaluations'] == 0
    assert all(trial['result'] == proxy for trial in report['trials'])
    assert all('must return BacktestResult' in trial['error'] for trial in report['trials'])


@pytest.mark.parametrize('damage', [
    'missing_baseline', 'zero_baseline', 'early_baseline', 'missing_day', 'early_eod',
    'late_equity', 'late_fill', 'nonfinite_equity', 'duplicate_fill', 'invalid_fee',
])
def test_invalid_scored_result_is_retained_as_failure_not_replaced_or_retried(
        dataset, grid, evaluator_factory, damage):
    def corrupt(result, subset, parameters, cost):
        if subset.role != 'test':
            return result
        index = len(subset.warmup_sessions) - 1
        if damage == 'missing_baseline':
            result.equity_curve.pop(index)
        elif damage == 'zero_baseline':
            result.equity_curve[index] = replace(result.equity_curve[index], equity=Decimal('0'))
        elif damage == 'early_baseline':
            result.equity_curve[index] = replace(result.equity_curve[index], at=result.equity_curve[index].at - timedelta(hours=1))
        elif damage == 'missing_day':
            result.equity_curve.pop(index + 1)
        elif damage == 'early_eod':
            result.equity_curve[index + 1] = replace(result.equity_curve[index + 1], at=subset.evaluation_start_at)
        elif damage == 'late_equity':
            result.equity_curve.append(replace(result.equity_curve[-1], at=subset.evaluation_end_at))
        elif damage == 'late_fill':
            result.fills.append(replace(result.fills[-1], fill_id='late', at=subset.evaluation_end_at))
        elif damage == 'nonfinite_equity':
            result.equity_curve[-1] = replace(result.equity_curve[-1], equity=Decimal('NaN'))
        elif damage == 'duplicate_fill':
            result.fills.append(result.fills[-1])
        elif damage == 'invalid_fee':
            result.fills[-1] = replace(result.fills[-1], commission=Decimal('-1'))
        return result

    evaluator = evaluator_factory(transform=corrupt)
    report = validate_strategy(dataset, evaluator, grid)
    trial = records(report, 'test')[0]
    assert trial['status'] == 'failed'
    assert trial['metrics'] is None
    assert trial['result'] is not None
    assert trial['error'].startswith('ValueError:')
    assert report['test_metrics'] is None
    assert report['test_evaluations'] == 1
    assert report['evaluation_count'] == 11
    assert any(f"Trial {trial['trial_id']}" in reason for reason in report['failure_reasons'])


def test_evaluator_cannot_mutate_registration_source_or_scoring_boundaries(dataset, grid, evaluator_factory):
    original_dataset, original_grid = deepcopy(dataset), deepcopy(grid)

    def mutate_inputs(result, subset, parameters, cost):
        subset.bars.clear()
        subset.actions.clear()
        subset.warmup_sessions.clear()
        subset.evaluation_sessions.clear()
        subset.evaluation_end_at = subset.evaluation_start_at
        parameters['lookback'] = 99999
        parameters['new'] = ['not preregistered']
        return result

    evaluator = evaluator_factory(transform=mutate_inputs)
    report = validate_strategy(dataset, evaluator, grid)
    assert dataset == original_dataset
    assert grid == original_grid
    assert report['registration']['parameter_grid'] == original_grid
    assert report['selected_parameters'] == original_grid[0]
    assert all(trial['status'] == 'completed' for trial in report['trials'])
    assert report['failure_reasons'] == []


def test_subsets_exclude_future_sessions_and_not_yet_available_bars_or_actions(dataset, grid, evaluator_factory):
    periods = chronological_splits(dataset, warmup_sessions=5)
    first, last = dataset.bars[5], dataset.bars[119]
    cutoff = last.close_at.replace(hour=0, minute=0) + timedelta(days=1, hours=4)
    # End of June in this fixture: next New York midnight is 04:00 UTC.
    dataset.bars.append(replace(last, symbol='DELAYED', available_at=cutoff))
    actions = [
        CorporateAction('KEPT', dataset.bars[40].session, first.open_at),
        CorporateAction('FUTURE_EX', dataset.bars[250].session, first.open_at),
        CorporateAction('LATE_KNOWLEDGE', dataset.bars[40].session, cutoff),
    ]
    dataset.actions.extend(actions)
    evaluator = evaluator_factory()
    report = validate_strategy(dataset, evaluator, grid)
    for subset, parameters, cost in evaluator.calls:
        role = 'validation' if subset.role == 'cost_stress' else subset.role
        expected = periods[role]
        assert subset.warmup_sessions == expected['warmup']
        assert subset.evaluation_sessions == expected['evaluation']
        assert subset.evaluation_start == expected['evaluation'][0]
        assert subset.evaluation_end == expected['evaluation'][-1]
        assert all(bar.session in expected['warmup'] + expected['evaluation'] for bar in subset.bars)
        assert all(bar.available_at < subset.evaluation_end_at for bar in subset.bars)
        assert all(expected['warmup'][0] <= action.ex_date <= expected['evaluation'][-1]
                   and action.known_at < subset.evaluation_end_at for action in subset.actions)
        assert subset.version.startswith(dataset.version + ':')
        assert subset.source == dataset.source and subset.synthetic is True
        assert subset.limitations == dataset.limitations
    train_subset = evaluator.calls[0][0]
    assert train_subset.evaluation_end_at == cutoff
    assert [action.symbol for action in train_subset.actions] == ['KEPT']
    assert 'DELAYED' not in {bar.symbol for bar in train_subset.bars}
    assert report['test_evaluations'] == 1


@pytest.mark.parametrize('bad_grid', [
    [], [{}, {}, {}, {}], [{}, {}], [{'lookback': 2}, {'lookback': 2}],
    'grid', 1, [1], [{1: 'bad'}], {1: [1]}, {'lookback': []}, {'lookback': 2},
    {'lookback': [1, 2, 3], 'skip': [0, 1]},
    [{'extra': float('nan')}], [{'extra': float('inf')}], [{'extra': object()}],
    [{'extra': Decimal('NaN')}], [{'extra': {'nested': [Decimal('Infinity')]}}],
])
def test_grid_is_finite_unique_and_bounded_before_any_evaluation(dataset, evaluator_factory, bad_grid):
    evaluator = evaluator_factory()
    with pytest.raises(ValueError):
        validate_strategy(dataset, evaluator, bad_grid)
    assert evaluator.calls == []


def test_lazy_or_unbounded_parameter_iterators_are_not_consumed(dataset, evaluator_factory):
    def unbounded():
        pytest.fail('Unbounded input must not be iterated')
        yield {}

    evaluator = evaluator_factory()
    with pytest.raises(ValueError, match='finite'):
        validate_strategy(dataset, evaluator, unbounded())
    with pytest.raises(ValueError, match='finite'):
        validate_strategy(dataset, evaluator, {'lookback': unbounded()})
    assert evaluator.calls == []


def test_finite_decimal_parameters_remain_supported_and_registered(dataset, grid, evaluator_factory):
    grid[0]['weight'] = Decimal('0.35')
    grid[1]['settings'] = {'weights': [Decimal('0.25'), Decimal('0.5')]}
    report = validate_strategy(dataset, evaluator_factory(), grid)
    assert report['registration']['parameter_grid'][0]['weight'] == '0.35'
    assert report['registration']['parameter_grid'][1]['settings']['weights'] == ['0.25', '0.5']
    assert report['evaluation_count'] == 11
    assert all(trial['status'] == 'completed' for trial in report['trials'])
    assert grid[0]['weight'] == Decimal('0.35')


def test_common_warmup_uses_largest_registered_lookback_skip_or_volatility_window(dataset, grid, evaluator_factory):
    grid[1]['skip'] = 8  # 3 + 8 + 1 = 12
    grid[2]['vol_lookback'] = 15  # 15 + 1 = 16, applies to every candidate
    evaluator = evaluator_factory()
    report = validate_strategy(dataset, evaluator, grid)
    assert report['registration']['warmup_sessions'] == 16
    for subset, parameters, cost in evaluator.calls:
        assert len(subset.warmup_sessions) == 16
    assert report['splits']['train']['evaluation_sample_size'] == 120 - 16


def test_grid_mapping_and_candidate_list_produce_same_registration(dataset, grid, evaluator_factory):
    mapping = {'lookback': [2, 3, 4], 'skip': [0], 'vol_lookback': [2]}
    left = validate_strategy(dataset, evaluator_factory(), mapping)
    right = validate_strategy(dataset, evaluator_factory(), grid)
    assert left == right


@pytest.mark.parametrize('changes', [
    {'lookback': 0}, {'lookback': True}, {'lookback': 1.5},
    {'skip': -1}, {'skip': False}, {'vol_lookback': 0}, {'vol_lookback': '2'},
])
def test_invalid_lookback_parameters_fail_before_evaluation(dataset, grid, evaluator_factory, changes):
    evaluator = evaluator_factory()
    with pytest.raises(ValueError, match='lookback'):
        validate_strategy(dataset, evaluator, [{**grid[0], **changes}])
    assert evaluator.calls == []


@pytest.mark.parametrize('seed', [True, 1.5, '7', None])
def test_invalid_seed_rejected_before_any_call(dataset, grid, evaluator_factory, seed):
    evaluator = evaluator_factory()
    with pytest.raises(ValueError, match='seed'):
        validate_strategy(dataset, evaluator, grid, seed=seed)
    assert evaluator.calls == []


def test_non_callable_evaluator_rejected(dataset, grid):
    with pytest.raises(ValueError, match='callable'):
        validate_strategy(dataset, None, grid)


def test_insufficient_split_history_produces_report_without_calls(dataset_factory, grid, evaluator_factory):
    evaluator = evaluator_factory()
    report = validate_strategy(dataset_factory(10), evaluator, grid)
    assert report['evaluation_count'] == report['test_evaluations'] == 0
    assert evaluator.calls == report['trials'] == []
    assert 'Insufficient chronological sessions' in report['failure_reasons'][0]
    assert report['passed'] is False
    assert report['verdict'] == 'NOT_VALIDATED_SYNTHETIC'


def test_short_but_splittable_sample_cannot_pass_and_explains_uncertainty(dataset_factory, grid, evaluator_factory):
    report = validate_strategy(dataset_factory(50), evaluator_factory(), grid)
    assert report['test_evaluations'] == 1
    assert report['test_metrics']['sample_size'] == 10
    assert report['uncertainty']['replications'] == 0
    assert report['uncertainty']['total_return_95ci'] is None
    assert report['uncertainty']['mean_daily_return_95ci'] is None
    assert 'at least 20' in report['uncertainty']['explanation']
    assert any('fewer than 20' in reason for reason in report['failure_reasons'])
    assert report['passed'] is False


def test_bootstrap_is_seeded_bounded_and_uses_only_scored_holdout_returns(dataset, grid, evaluator_factory):
    first = validate_strategy(dataset, evaluator_factory(), grid, seed=17)
    repeated = validate_strategy(dataset, evaluator_factory(), grid, seed=17)
    different = validate_strategy(dataset, evaluator_factory(), grid, seed=18)
    assert first == repeated
    uncertainty = first['uncertainty']
    assert uncertainty['sample_size'] == first['test_metrics']['sample_size'] == 60
    assert uncertainty['replications'] == 200
    assert uncertainty['block_length'] == math.ceil(math.sqrt(60))
    assert uncertainty['seed'] == 17
    assert 'not search-adjusted' in uncertainty['method']
    for key in ('total_return_95ci', 'mean_daily_return_95ci'):
        low, high = uncertainty[key]
        assert 0 < low <= high
        assert math.isfinite(low) and math.isfinite(high)
    assert first['registration']['fingerprint'] != different['registration']['fingerprint']
    assert first['test_metrics'] == different['test_metrics']
    assert first['evaluation_count'] == repeated['evaluation_count'] == different['evaluation_count'] == 11


def test_positive_holdout_total_cannot_pass_when_bootstrap_mean_includes_losses(dataset, grid, evaluator_factory):
    dataset.synthetic = False  # exercise uncertainty gate independently of the synthetic veto

    def uncertain_holdout(result, subset, parameters, cost):
        if subset.role == 'test':
            warmup = len(subset.warmup_sessions)
            equity = result.equity_curve[warmup - 1].equity
            for index in range(len(subset.evaluation_sessions)):
                rate = Decimal('-0.03') if index < 29 else Decimal('0.031')
                equity = equity * (1 + rate) - (Decimal('1') if index == 0 else 0)
                result.equity_curve[warmup + index] = replace(
                    result.equity_curve[warmup + index], cash=equity, equity=equity)
        return result

    evaluator = evaluator_factory(transform=uncertain_holdout, result_synthetic=False)
    report = validate_strategy(dataset, evaluator, grid)
    assert report['test_metrics']['total_return'] > 0
    assert report['uncertainty']['mean_daily_return_95ci'][0] < 0
    assert report['passed'] is False
    assert report['verdict'] == 'FAILED_RESEARCH_SCREEN'
    assert report['failure_reasons'] == ['Holdout uncertainty is unavailable or includes a nonpositive mean return']
    assert report['test_evaluations'] == 1
    assert len(evaluator.calls) == 11


@pytest.mark.parametrize(('factor', 'phase'), [('1.01', 'up'), ('0.99', 'down'), ('1', 'sideways')])
def test_phase_performance_uses_lagged_market_data_and_only_rolling_and_test_scores(
        dataset, grid, evaluator_factory, factor, phase):
    dataset.bars = [replace(bar, adjusted_close=Decimal('100') * Decimal(factor) ** index)
                    for index, bar in enumerate(dataset.bars)]
    report = validate_strategy(dataset, evaluator_factory(), grid)
    result = report['phase_performance']
    assert result['reference_symbol'] == 'SPY'
    assert result['lookback'] == 20 and result['threshold'] == 0.02
    assert 'known before scored session open' in result['definition']
    assert 'no retuning' in result['scope']
    assert set(result['phases']) == {'up', 'down', 'sideways', 'unknown'}
    assert result['phases'][phase]['sample_size'] == 30 + 30 + 60
    expected = math.prod(1 + trial['metrics']['total_return'] for trial in report['trials']
                         if trial['role'] in ('rolling_1', 'rolling_2', 'test')) - 1
    assert result['phases'][phase]['conditional_compounded_return'] == pytest.approx(expected)
    assert result['phases'][phase]['mean_daily_return'] == pytest.approx((3 * 0.001 + 117 * 0.002) / 120)
    for name, values in result['phases'].items():
        if name != phase:
            assert values['sample_size'] == 0
            assert values['conditional_compounded_return'] is None
            assert values['mean_daily_return'] is None
            assert values['volatility'] is None
            assert values['explanation']
    assert report['passed'] is False  # phase patterns are fixtures, not strategy evidence


def test_phase_classifier_does_not_read_future_available_reference_prices(dataset, grid, evaluator_factory):
    # QQQ supplies a scored session even when the SPY observation is unavailable.
    dataset.bars += [replace(bar, symbol='QQQ') for bar in dataset.bars]
    dataset.bars[190] = replace(dataset.bars[190], adjusted_close=Decimal('10000'),
                                available_at=dataset.bars[-1].close_at + timedelta(days=2))
    report = validate_strategy(dataset, evaluator_factory(), grid)
    phases = report['phase_performance']['phases']
    assert phases['sideways']['sample_size'] == 120
    assert phases['up']['sample_size'] == phases['down']['sample_size'] == 0
    assert all(trial['status'] == 'completed' for trial in report['trials'])


def test_insufficient_reference_history_is_unknown_not_a_fabricated_phase(dataset, grid, evaluator_factory):
    dataset.bars = [replace(bar, symbol='QQQ') if index >= 10 else bar
                    for index, bar in enumerate(dataset.bars)]
    report = validate_strategy(dataset, evaluator_factory(), grid)
    phases = report['phase_performance']
    assert phases['reference_symbol'] == 'SPY'
    assert phases['phases']['unknown']['sample_size'] == 120
    assert sum(row['sample_size'] for row in phases['phases'].values()) == 120


def test_disclosures_preserve_data_limits_and_do_not_claim_global_holdout_sealing(dataset, grid, evaluator_factory):
    report = validate_strategy(dataset, evaluator_factory(), grid)
    assert all(limit in report['limitations'] for limit in dataset.limitations)
    disclosures = ' '.join(report['limitations'])
    for wording in ('no claim', 'Synthetic', 'point-in-time', 'externally', 'cross-run', 'spread/slippage'):
        assert wording in disclosures
