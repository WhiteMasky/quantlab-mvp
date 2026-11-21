"""Local, versioned research plans, declared independently of evaluation output.

These are protocol specifications, not generated signals or performance claims.
They do not constitute an independently witnessed, timestamped preregistration.
The experiment's code_version must identify the actual protocol and code used.
"""
from __future__ import annotations

from copy import deepcopy


_COMMON = {
    'protocol_version': 'quantlab-research-v1',
    'predeclared': True,
    'preregistration_scope': 'Local versioned specification; not external preregistration.',
    'benchmark_specs': [
        {
            'name': 'cash',
            'description': 'Uninvested USD cash, assumed zero interest; disclose this assumption.',
        },
        {
            'name': 'SPY_buy_and_hold',
            'description': (
                'Buy-and-hold SPY on the same scored dates and starting capital, using raw '
                'executable prices, corporate actions and the same hypothetical costs. '
                'If SPY is absent, report unavailable; do not substitute a winning asset.'
            ),
        },
        {
            'name': 'equal_weight_buy_and_hold',
            'description': (
                'Equal initial allocations to the predeclared eligible universe, then '
                'buy-and-hold, with whole-share cash residuals and identical costs/dates. '
                'Freeze membership before evaluation; disclose missing historical membership.'
            ),
        },
    ],
    'evaluation_protocol': {
        'split': 'Chronological train / validation / untouched test; never shuffle observations.',
        'warmup': 'Prior observations may warm up indicators but cannot be scored in the next period.',
        'selection': (
            'Choose parameters on validation only under a criterion fixed before evaluation; '
            'evaluate exactly one chosen configuration per hypothesis on the final holdout. '
            'Report both hypotheses, not only the better holdout result.'
        ),
        'selection_metric': 'Validation net-of-cost total return, descending; non-finite values ineligible.',
        'selection_tie_break': 'First eligible configuration in the predeclared parameter_grid.',
        'no_eligible_configuration': 'Report inconclusive; do not invent a score or expand the grid.',
        'holdout_optimization': False,
        'primary_evaluation_budget_per_candidate': 9,
        'budget_scope': (
            'Per hypothesis: two train, two validation, two rolling OOS, two cost stress '
            'and one final holdout evaluation; all nine attempts retained.'
        ),
        'diagnostics': [
            'Chronological rolling out-of-sample and predeclared market-phase results.',
            'Cost stress at multipliers 1, 2 and 3; fees are hypothetical assumptions, not broker tariffs.',
            'Neighbor-parameter sensitivity restricted to the declared grid; no holdout-driven expansion.',
            'Seeded block-bootstrap uncertainty; report resample and total search/evaluation counts.',
        ],
        'retention': (
            'Start every evaluation before computing it; retain all parameters, seeds, versions, '
            'running records, failures, negative returns and diagnostics without score-based filtering.'
        ),
    },
    'statistical_limitations': [
        'A hypothesis is not evidence of edge; positive backtests do not establish statistical significance.',
        'Two hypotheses and every searched configuration incur multiple-testing and selection bias.',
        'Short samples, serial dependence and regime changes weaken Sharpe and bootstrap inference.',
        'Survivorship bias, missing delistings and unavailable historical universe membership limit conclusions.',
        'Retrospectively adjusted prices may lack point-in-time validity; availability gates alone do not repair this.',
        'Execution costs, liquidity, opening fills and corporate-action handling are modeled approximations.',
        'Warmup/scored periods and unavailable benchmarks must be explicit; never silently backfill them.',
        'A holdout can be used once for confirmation, not repeated tuning; all attempts must remain visible.',
    ],
    'synthetic_policy': {
        'passed': False,
        'verdict': 'NOT_VALIDATED_SYNTHETIC',
        'description': 'Synthetic data tests software only and never supplies investment evidence.',
    },
}


def candidate_specs() -> list[dict]:
    """Return independent JSON-compatible copies of exactly two fixed candidates.

    Each grid has only two configurations, in deterministic order. Defaults and
    nuisance parameters are fixed, not inferred from results or the universe.
    Actual signals belong to the strategy module; this function performs no
    data access, parameter search, backtest, benchmark computation or execution.
    """
    candidates = [
        {
            'strategy': 'trend',
            'name': 'Time-series trend',
            'hypothesis': (
                'Positive own-asset trailing adjusted-price trends may persist. A long-only '
                'positive-trend filter may improve net risk-adjusted performance versus '
                'cash and passive exposure; this is an unproven, falsifiable hypothesis.'
            ),
            'signal_policy': (
                'Use only observations available at the decision timestamp. Each asset is '
                'evaluated against its own history, not cross-sectional rank. Nonpositive '
                'signals or insufficient history do not justify a position; remain in cash '
                'when no candidate qualifies. Raw prices, not adjusted prices, execute trades.'
            ),
            'parameter_space': {'lookback': [63, 126], 'skip': [5], 'top_n': [2]},
            'parameter_grid': [
                {'lookback': 63, 'skip': 5, 'top_n': 2},
                {'lookback': 126, 'skip': 5, 'top_n': 2},
            ],
            'default_parameters': {'lookback': 63, 'skip': 5, 'top_n': 2},
            'fixed_parameter_note': 'skip and top_n are fixed interface arguments, not trend tuning axes.',
        },
        {
            'strategy': 'momentum',
            'name': 'Cross-sectional momentum',
            'hypothesis': (
                'Assets with stronger trailing adjusted returns relative to the same eligible '
                'universe, excluding the most recent five observations, may continue to '
                'outperform weaker peers after costs. This is unproven, not a promised premium.'
            ),
            'signal_policy': (
                'Rank only eligible, prior-available histories; prefer the top two positive '
                'momentum candidates, with lexical symbol ties. Do not short losing assets. '
                'Missing/insufficient histories remain ineligible and unallocated capital is '
                'cash. Signal eligibility is not an order; portfolio and risk controls still apply.'
            ),
            'parameter_space': {'lookback': [63, 126], 'skip': [5], 'top_n': [2]},
            'parameter_grid': [
                {'lookback': 63, 'skip': 5, 'top_n': 2},
                {'lookback': 126, 'skip': 5, 'top_n': 2},
            ],
            'default_parameters': {'lookback': 63, 'skip': 5, 'top_n': 2},
            'fixed_parameter_note': 'Five-observation skip and two-asset selection are not optimized.',
        },
    ]
    return [{**deepcopy(_COMMON), **candidate} for candidate in candidates]
