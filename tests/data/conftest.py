from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantlab.data import dataset_version, session_times
from quantlab.models import CorporateAction, Dataset, MarketBar


@pytest.fixture
def sample_dataset():
    days = (date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 12))
    bars = []
    for index, day in enumerate(days):
        opening, closing = session_times(day)
        for symbol, prices in (('SPY', ('100.0000', '50.0000', '51.0000')),
                               ('QQQ', ('200.0000', '202.0000', '201.0000'))):
            price = Decimal(prices[index])
            bars.append(MarketBar(
                symbol, day, opening, closing,
                closing + timedelta(minutes=1, microseconds=123456),
                price, price + Decimal('1'), price - Decimal('1'), price,
                price / Decimal('2') if index == 0 else price, 1_000_000 + index,
            ))
    actions = [
        CorporateAction('SPY', days[1], session_times(days[0])[1] + timedelta(minutes=1),
                        Decimal('2.0000'), Decimal('0.2500')),
        CorporateAction('QQQ', days[2], session_times(days[1])[1] + timedelta(minutes=1),
                        Decimal('1'), Decimal('0.50')),
    ]
    dataset = Dataset('', 'LOCAL synthetic fixture / 测试', True, bars, actions,
                      ['Software tests only; not investment evidence.',
                       'Historical universe/delistings and point-in-time adjustments are unverified.'])
    dataset.version = dataset_version(dataset)
    return dataset


@pytest.fixture
def as_of(sample_dataset):
    return max(bar.available_at for bar in sample_dataset.bars)
