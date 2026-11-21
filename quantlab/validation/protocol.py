"""Bounded preregistered screening, not evidence of an investable statistical edge."""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from statistics import fmean, stdev

from quantlab.models import BacktestResult, Dataset, jsonable, require_utc
from .metrics import NEW_YORK, _daily_points, _number, _returns, performance_metrics

MAX_CANDIDATES = 3
MIN_SCREEN_SAMPLES = 20
BOOTSTRAP_REPLICATIONS = 200


@dataclass
class EvaluationDataset(Dataset):
    """The evaluator may run all bars, but MUST respect their availability gates.

    Warmup bars are historical context, never independently scored. State may
    carry from warmup into evaluation. Scoring rebases to the last warmup EOD
    equity and drops warmup fills. No reset or initial-cash guess is made.
    End is inclusive by session; end_at is exclusive in UTC. This metadata is
    optional for evaluators: an ordinary full-curve run_backtest is sufficient.
    """
    evaluation_start: date | None = None
    evaluation_end: date | None = None
    evaluation_start_at: datetime | None = None
    evaluation_end_at: datetime | None = None
    warmup_sessions: list[date] = field(default_factory=list)
    evaluation_sessions: list[date] = field(default_factory=list)
    role: str = ''


def _end_at(day):
    return datetime.combine(day + timedelta(days=1), time(), NEW_YORK).astimezone(timezone.utc)


def _grid(parameter_grid):
    """Materialize only finite, bounded registrations, before ANY evaluator call."""
    if isinstance(parameter_grid, dict):
        if any(not isinstance(key, str) for key in parameter_grid):
            raise ValueError('Parameter names must be strings')
        values = list(parameter_grid.values())
        if any(not isinstance(value, (list, tuple)) or not value for value in values):
            raise ValueError('Grid mapping values must be nonempty finite lists/tuples')
        if math.prod(len(value) for value in values) > MAX_CANDIDATES:
            raise ValueError(f'Preregister at most {MAX_CANDIDATES} candidates')
        candidates = [dict(zip(parameter_grid, row)) for row in itertools.product(*values)]
    elif isinstance(parameter_grid, (list, tuple)):
        candidates = list(parameter_grid)
    else:
        raise ValueError('Grid must be a finite list of dictionaries or a mapping of finite lists')
    if not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError(f'Preregister 1..{MAX_CANDIDATES} candidates; holdout tuning is forbidden')
    if any(not isinstance(row, dict) or any(not isinstance(key, str) for key in row) for row in candidates):
        raise ValueError('Each candidate must be a dictionary with string parameter names')
    candidates = copy.deepcopy(candidates)

    def check_decimals(value):
        # jsonable renders Decimal as strings, so allow_nan=False alone cannot
        # detect Decimal NaN/Infinity, including inside nested parameters.
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError('Non-finite Decimal parameter')
        if isinstance(value, dict):
            for item in value.values():
                check_decimals(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                check_decimals(item)

    try:
        check_decimals(candidates)
        encoded = [json.dumps(jsonable(row), sort_keys=True, allow_nan=False) for row in candidates]
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('Parameters must have finite JSON-serializable values') from exc
    if len(set(encoded)) != len(encoded):
        raise ValueError('Duplicate candidates would misstate the search count')
    return candidates


def _warmup(candidates):
    lengths = []
    for params in candidates:
        lookback = params.get('lookback', 63)
        skip = params.get('skip', 5)
        vol = params.get('vol_lookback', 20)
        if any(type(value) is not int for value in (lookback, skip, vol)) or lookback < 1 or skip < 0 or vol < 1:
            raise ValueError('lookback/vol_lookback must be positive integers; skip a nonnegative integer')
        lengths.append(max(lookback + skip + 1, vol + 1))
    return max(lengths)


def chronological_splits(dataset, warmup_sessions=69) -> dict:
    """40/20/20/20 chronological train/selection/rolling/test allocation.

    Rolling windows are genuinely AFTER selection and BEFORE sealed test, not
    retrospectively scored training folds using validation-selected parameters.
    Train's initial warmup is separate; each subsequent window receives at most
    the same preregistered number of immediately preceding historical sessions.
    """
    if type(warmup_sessions) is not int or warmup_sessions < 1:
        raise ValueError('At least one historical warmup session is required')
    days = sorted({bar.session for bar in dataset.bars})
    n = len(days)
    a, b, c, d = (n * value // 10 for value in (4, 6, 7, 8))
    ranges = {'train': (warmup_sessions, a), 'validation': (a, b),
              'rolling_1': (b, c), 'rolling_2': (c, d), 'test': (d, n)}
    if any(end - start < 2 for start, end in ranges.values()):
        raise ValueError('Insufficient chronological sessions after the preregistered training warmup')
    result = {}
    for role, (start, end) in ranges.items():
        warmup = days[max(0, start - warmup_sessions):start]
        result[role] = {'warmup': warmup, 'evaluation': days[start:end]}
    return result


def _subset(dataset, period, role):
    warmup, evaluation = period['warmup'], period['evaluation']
    allowed = set(warmup + evaluation)
    cutoff = _end_at(evaluation[-1])
    bars = sorted((bar for bar in dataset.bars if bar.session in allowed and bar.available_at < cutoff),
                  key=lambda bar: (bar.session, bar.symbol, bar.available_at))
    # Both ex-date and knowledge gates protect early folds from future actions.
    actions = [action for action in dataset.actions
               if warmup[0] <= action.ex_date <= evaluation[-1] and action.known_at < cutoff]
    opens = [bar.open_at for bar in bars if bar.session == evaluation[0]]
    if not opens:
        raise ValueError('No available observations at the evaluation boundary')
    suffix = hashlib.sha256(f'{dataset.version}|{role}|{warmup[0]}|{evaluation[-1]}'.encode()).hexdigest()[:16]
    return EvaluationDataset(
        version=f'{dataset.version}:{suffix}', source=dataset.source, synthetic=dataset.synthetic,
        bars=copy.deepcopy(bars), actions=copy.deepcopy(actions), limitations=list(dataset.limitations),
        evaluation_start=evaluation[0], evaluation_end=evaluation[-1],
        evaluation_start_at=min(opens), evaluation_end_at=cutoff,
        warmup_sessions=list(warmup), evaluation_sessions=list(evaluation), role=role,
    )


def _score(result, subset):
    if not isinstance(result, BacktestResult):
        raise ValueError('Evaluator must return BacktestResult, not a metric or scalar proxy')
    daily = _daily_points(result.equity_curve)
    by_day = {point.at.astimezone(NEW_YORK).date(): point for point in daily}
    if any(point.at >= subset.evaluation_end_at for point in daily):
        raise ValueError('Evaluator returned observations beyond its sealed period')
    baseline_day = subset.warmup_sessions[-1]
    baseline = by_day.get(baseline_day)
    if baseline is None or _number(baseline.equity) is None or baseline.equity <= 0:
        raise ValueError('Missing positive last-warmup EOD equity; starting capital cannot be guessed')
    points = []
    close_times = {}
    for bar in subset.bars:
        close_times[bar.session] = max(close_times.get(bar.session, bar.close_at), bar.close_at)
    if baseline_day not in close_times or baseline.at < close_times[baseline_day]:
        raise ValueError('Warmup baseline must be an end-of-session snapshot')
    for day in subset.evaluation_sessions:
        point = by_day.get(day)
        if point is None or day not in close_times or point.at < close_times[day]:
            raise ValueError(f'Missing scored end-of-session equity: {day}')
        points.append(point)
    fills = []
    for fill in result.fills:
        require_utc(fill.at)
        if fill.at >= subset.evaluation_end_at:
            raise ValueError('Evaluator returned executions beyond its sealed period')
        if subset.evaluation_start_at <= fill.at < subset.evaluation_end_at:
            fills.append(fill)
    metrics = performance_metrics(points, fills, baseline.equity)
    returns = _returns(points, baseline.equity)
    if metrics['total_return'] is None or returns is None:
        raise ValueError('Scored capital path cannot produce finite daily returns')
    # Undefined Sharpe/Sortino can be legitimate, but invalid execution data
    # cannot support a net-of-cost screen. Retain the full failed result instead.
    if any(metrics[key] is None for key in ('turnover', 'fee_ratio')):
        errors = [metrics['explanations'][key] for key in ('turnover', 'fee_ratio')
                  if metrics[key] is None]
        raise ValueError('Invalid scored execution accounting: ' + '; '.join(dict.fromkeys(errors)))
    return metrics, points, returns


def _bootstrap(returns, seed):
    description = 'Seeded circular moving-block bootstrap; descriptive, not search-adjusted or proof of edge'
    if len(returns) < MIN_SCREEN_SAMPLES:
        return {'method': description, 'sample_size': len(returns), 'replications': 0,
                'total_return_95ci': None, 'mean_daily_return_95ci': None,
                'explanation': f'Requires at least {MIN_SCREEN_SAMPLES} scored holdout returns'}
    rng = random.Random(seed)
    n, reps = len(returns), BOOTSTRAP_REPLICATIONS
    block = max(2, math.ceil(math.sqrt(n)))
    totals, means = [], []
    for _ in range(reps):
        sample = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(returns[(start + j) % n] for j in range(min(block, n - len(sample))))
        totals.append(math.prod(1 + value for value in sample) - 1)
        means.append(fmean(sample))
    def interval(values):
        values.sort()
        if any(not math.isfinite(value) for value in values):
            return None
        return [values[math.floor(.025 * (reps - 1))], values[math.ceil(.975 * (reps - 1))]]
    return {'method': description, 'sample_size': n, 'replications': reps, 'seed': seed,
            'block_length': block, 'total_return_95ci': interval(totals),
            'mean_daily_return_95ci': interval(means)}


def _phases(dataset, scored):
    symbols = sorted({bar.symbol for bar in dataset.bars})
    symbol = 'SPY' if 'SPY' in symbols else (symbols[0] if symbols else None)
    reference = sorted((bar for bar in dataset.bars if bar.symbol == symbol), key=lambda bar: bar.session)
    groups = {phase: [] for phase in ('up', 'down', 'sideways', 'unknown')}
    for points, returns in scored:
        for point, value in zip(points, returns):
            day = point.at.astimezone(NEW_YORK).date()
            opens = [bar.open_at for bar in dataset.bars if bar.session == day]
            history = [bar for bar in reference if bar.session < day and opens and bar.available_at <= min(opens)]
            phase = 'unknown'
            if len(history) >= 21:
                earlier, latest = _number(history[-21].adjusted_close), _number(history[-1].adjusted_close)
                if earlier is not None and latest is not None and earlier > 0 and latest > 0:
                    momentum = latest / earlier - 1
                    phase = 'up' if momentum > .02 else 'down' if momentum < -.02 else 'sideways'
            groups[phase].append(value)
    result = {}
    for phase, values in groups.items():
        compounded = math.prod(1 + value for value in values) - 1 if values else None
        result[phase] = {
            'sample_size': len(values),
            'conditional_compounded_return': compounded if compounded is None or math.isfinite(compounded) else None,
            'mean_daily_return': fmean(values) if values else None,
            'volatility': stdev(values) * math.sqrt(252) if len(values) >= 2 else None,
            'explanation': None if len(values) >= 2 else 'Insufficient observations for phase volatility',
        }
    return {'reference_symbol': symbol, 'lookback': 20, 'threshold': .02,
            'definition': 'Lagged 20-session adjusted-close momentum, known before scored session open',
            'scope': 'Selected parameters on rolling OOS and final test only; descriptive, no retuning',
            'limitation': 'Noncontiguous conditional returns are not an investable equity curve; adjusted-data PIT limits apply',
            'phases': result}


def validate_strategy(dataset, evaluator, parameter_grid, seed=7) -> dict:
    """Run an immutable, <=11-call protocol with exactly one final holdout attempt.

    Three candidates maximum. Selection maximizes validation total return only;
    input order breaks ties. Training results, OOS, stress tests and holdout do
    not select parameters. A failed call is retained and never retried. Costs
    2/3 are applied on validation only, never by reopening the test. Sensitivity
    reuses preregistered validation trials (not an adaptive neighborhood search).
    Repeated invocations are separate research searches: callers must persist
    this report and enforce dataset holdout governance across invocations.
    """
    candidates = _grid(parameter_grid)
    warmup = _warmup(candidates)
    if type(seed) is not int:
        raise ValueError('seed must be an integer')
    if not callable(evaluator):
        raise ValueError('evaluator must be callable')
    for bar in dataset.bars:
        for stamp in (bar.open_at, bar.close_at, bar.available_at):
            require_utc(stamp)
    for action in dataset.actions:
        require_utc(action.known_at)
    registration = {
        'parameter_grid': jsonable(candidates), 'seed': seed, 'warmup_sessions': warmup,
        'selection_metric': 'validation.total_return', 'tie_break': 'first preregistered candidate',
        'cost_multipliers': [1.0, 2.0, 3.0], 'maximum_primary_evaluations': 2 * len(candidates) + 5,
        'split_fractions': {'train': .4, 'validation': .2, 'rolling_oos': .2, 'test': .2},
        'screen': {'minimum_samples_per_window': MIN_SCREEN_SAMPLES,
                   'positive_returns_all_windows_and_costs': True,
                   'all_preregistered_validation_candidates_positive': True,
                   'holdout_mean_bootstrap_lower_bound_positive': True},
    }
    registration['fingerprint'] = hashlib.sha256(json.dumps(registration, sort_keys=True).encode()).hexdigest()
    report = {
        'passed': False, 'verdict': 'NOT_VALIDATED', 'synthetic': bool(dataset.synthetic),
        'data_version': dataset.version, 'registration': registration, 'splits': {},
        'selected_parameters': None, 'selected_candidate': None, 'selection_locked': False,
        'trials': [], 'search_count': len(candidates), 'evaluation_count': 0, 'test_evaluations': 0,
        'rolling_oos': [], 'cost_stress': [], 'parameter_sensitivity': {}, 'phase_performance': {},
        'uncertainty': _bootstrap([], seed), 'test_metrics': None, 'failure_reasons': [],
        'limitations': list(dataset.limitations) + [
            'Research screen only; no claim of statistical edge or future profitability.',
            'Synthetic prices never count as investment evidence.',
            'Historical universe, survivorship/delistings and point-in-time adjusted-data limits are not repaired here.',
            'Evaluator is supplied externally: availability gating and realistic execution require independent auditing.',
            'Warmup may carry positions; returns rebase at its last EOD equity and exclude its commissions.',
            'Annualization assumes consecutive daily trading observations; missing source sessions cannot be inferred.',
            'Commission fee_ratio excludes separately unreported spread/slippage components.',
            'Bootstrap is small-sample descriptive, not multiple-search adjusted; prior/external searches are unknown.',
            'Holdout is single-use within this invocation only; caller must persist registration and enforce cross-run sealing.',
        ],
    }

    def finish():
        if report['synthetic']:
            report['passed'] = False
            report['verdict'] = 'NOT_VALIDATED_SYNTHETIC'
        elif report['passed']:
            report['verdict'] = 'PASSED_RESEARCH_SCREEN'
        elif report['test_evaluations']:
            report['verdict'] = 'FAILED_RESEARCH_SCREEN'
        return report

    try:
        periods = chronological_splits(dataset, warmup)
    except ValueError as exc:
        report['failure_reasons'].append(str(exc))
        return finish()
    for role, period in periods.items():
        report['splits'][role] = {
            'warmup_start': period['warmup'][0].isoformat(), 'warmup_end': period['warmup'][-1].isoformat(),
            'warmup_sample_size': len(period['warmup']),
            'evaluation_start': period['evaluation'][0].isoformat(),
            'evaluation_end': period['evaluation'][-1].isoformat(),
            'evaluation_sample_size': len(period['evaluation']), 'end_inclusive': True,
        }
    private_scores = {}

    def evaluate(role, index, cost=1.0):
        record = {'trial_id': len(report['trials']) + 1, 'role': role, 'candidate_index': index,
                  'parameters': jsonable(candidates[index]), 'cost_multiplier': cost,
                  'status': 'failed', 'metrics': None, 'result': None, 'error': None}
        report['trials'].append(record)
        period_role = 'validation' if role == 'cost_stress' else role
        record['period'] = copy.deepcopy(report['splits'][period_role])
        try:
            subset = _subset(dataset, periods[period_role], role)
            report['evaluation_count'] += 1
            if role == 'test':
                report['test_evaluations'] += 1
            # Preserve the scoring boundaries even if an external evaluator mutates its inputs.
            scoring_subset = copy.deepcopy(subset)
            result = evaluator(subset, copy.deepcopy(candidates[index]), cost)
            record['result'] = jsonable(result)
            if isinstance(result, BacktestResult) and result.synthetic:
                report['synthetic'] = True
            metrics, points, returns = _score(result, scoring_subset)
            record['metrics'], record['status'] = metrics, 'completed'
            private_scores[record['trial_id']] = (points, returns)
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
        return record

    train = [evaluate('train', index) for index in range(len(candidates))]
    validation = [evaluate('validation', index) for index in range(len(candidates))]
    eligible = [record for record in validation if record['status'] == 'completed']
    report['parameter_sensitivity'] = {
        'method': 'All preregistered validation candidates, reused without additional evaluations',
        'trials': [{'trial_id': record['trial_id'], 'parameters': record['parameters'],
                    'total_return': record['metrics']['total_return'] if record['metrics'] else None,
                    'status': record['status']} for record in validation],
        'explanation': 'Only one candidate; sensitivity is not identifiable' if len(candidates) == 1 else None,
    }
    if not eligible:
        report['failure_reasons'].append('No valid validation candidate; sealed test was not accessed')
        return finish()
    winner = max(eligible, key=lambda record: record['metrics']['total_return'])
    chosen = winner['candidate_index']
    report['selected_candidate'] = chosen
    report['selected_parameters'] = jsonable(copy.deepcopy(candidates[chosen]))
    report['selection_locked'] = True
    report['selection_trial_id'] = winner['trial_id']
    # No decisions about the selected candidate are made below this boundary.
    rolling = [evaluate(role, chosen) for role in ('rolling_1', 'rolling_2')]
    costs = [winner] + [evaluate('cost_stress', chosen, cost) for cost in (2.0, 3.0)]
    report['rolling_oos'] = [record['trial_id'] for record in rolling]
    report['cost_stress'] = [record['trial_id'] for record in costs]
    test = evaluate('test', chosen)  # Last evaluator call, irrespective of the result.
    report['test_trial_id'], report['test_metrics'] = test['trial_id'], test['metrics']
    if test['status'] == 'completed':
        report['uncertainty'] = _bootstrap(private_scores[test['trial_id']][1], seed)
    report['phase_performance'] = _phases(dataset, [private_scores[record['trial_id']]
                                                  for record in rolling + [test] if record['status'] == 'completed'])
    required = train + validation + rolling + costs[1:] + [test]
    for record in required:
        if record['status'] != 'completed':
            report['failure_reasons'].append(f"Trial {record['trial_id']} ({record['role']}) failed: {record['error']}")
        elif record['metrics']['sample_size'] < MIN_SCREEN_SAMPLES:
            report['failure_reasons'].append(f"Trial {record['trial_id']} has fewer than {MIN_SCREEN_SAMPLES} scored sessions")
    # Training is diagnostic: never used for parameter ranking or profitability screening.
    for record in validation + rolling + costs[1:] + [test]:
        if record['metrics'] and record['metrics']['total_return'] <= 0:
            report['failure_reasons'].append(f"Nonpositive {record['role']} return in trial {record['trial_id']}")
    ci = report['uncertainty']['mean_daily_return_95ci']
    if ci is None or ci[0] <= 0:
        report['failure_reasons'].append('Holdout uncertainty is unavailable or includes a nonpositive mean return')
    if len(candidates) < 2:
        report['failure_reasons'].append('Parameter sensitivity requires at least two preregistered candidates')
    report['passed'] = not report['failure_reasons']
    return finish()
