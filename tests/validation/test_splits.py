"""Chronological separation by session, independent of data-source modules."""
from copy import deepcopy
from dataclasses import replace

import pytest

from quantlab.validation import chronological_splits


@pytest.mark.parametrize('count', [100, 101, 109])
def test_exact_chronological_ranges_and_immediately_preceding_warmup(dataset_factory, count):
    dataset = dataset_factory(count)
    days = [bar.session for bar in dataset.bars]
    boundaries = [count * fraction // 10 for fraction in (4, 6, 7, 8)]
    a, b, c, d = boundaries
    expected = {'train': (10, a), 'validation': (a, b), 'rolling_1': (b, c),
                'rolling_2': (c, d), 'test': (d, count)}
    splits = chronological_splits(dataset, warmup_sessions=10)
    assert list(splits) == list(expected)
    scored = []
    for role, (start, end) in expected.items():
        assert splits[role]['evaluation'] == days[start:end]
        assert splits[role]['warmup'] == days[start - 10:start]
        assert splits[role]['warmup'][-1] < splits[role]['evaluation'][0]
        assert set(splits[role]['warmup']).isdisjoint(splits[role]['evaluation'])
        scored.extend(splits[role]['evaluation'])
    assert scored == days[10:]
    assert len(scored) == len(set(scored))
    assert splits['validation']['evaluation'][-1] < splits['rolling_1']['evaluation'][0]
    assert splits['rolling_2']['evaluation'][-1] < splits['test']['evaluation'][0]


def test_split_counts_unique_sessions_not_symbol_bars_and_does_not_mutate(dataset_factory):
    dataset = dataset_factory(100)
    expected = chronological_splits(dataset, 10)
    dataset.bars += [replace(bar, symbol='QQQ') for bar in dataset.bars]
    dataset.bars.reverse()
    before = deepcopy(dataset)
    assert chronological_splits(dataset, 10) == expected
    assert dataset == before


def test_default_warmup_is_explicit_and_excluded_from_training(dataset):
    splits = chronological_splits(dataset)
    assert len(splits['train']['warmup']) == 69
    assert len(splits['train']['evaluation']) == 51
    assert splits['train']['evaluation'][0] == dataset.bars[69].session
    assert len(splits['validation']['evaluation']) == 60
    assert len(splits['rolling_1']['evaluation']) == 30
    assert len(splits['rolling_2']['evaluation']) == 30
    assert len(splits['test']['evaluation']) == 60


def test_minimal_two_observation_windows_are_splittable_not_yet_validated(dataset_factory):
    splits = chronological_splits(dataset_factory(18), warmup_sessions=1)
    assert [len(period['evaluation']) for period in splits.values()] == [6, 3, 2, 2, 4]


@pytest.mark.parametrize('warmup', [0, -1, True, 1.5, '10', None])
def test_invalid_warmup_rejected(dataset, warmup):
    with pytest.raises(ValueError, match='warmup'):
        chronological_splits(dataset, warmup)


@pytest.mark.parametrize(('count', 'warmup'), [(0, 1), (10, 1), (17, 1), (100, 39), (100, 100)])
def test_insufficient_history_never_falls_back_to_overlapping_windows(dataset_factory, count, warmup):
    with pytest.raises(ValueError, match='Insufficient chronological sessions'):
        chronological_splits(dataset_factory(count), warmup)
