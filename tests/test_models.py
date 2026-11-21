from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quantlab.models import OrderIntent, jsonable, money, require_utc


def test_live_order_cannot_be_constructed():
    with pytest.raises(ValueError, match='Live orders'):
        OrderIntent('i', 'c', 'SPY', 'BUY', 1, datetime.now(timezone.utc), Decimal('100'), mode='LIVE')


@pytest.mark.parametrize('quantity', [0, -1, 1.5, True])
def test_whole_share_positive_quantity(quantity):
    with pytest.raises(ValueError):
        OrderIntent('i', 'c', 'SPY', 'BUY', quantity, datetime.now(timezone.utc), Decimal('100'))


def test_utc_and_precision():
    with pytest.raises(ValueError):
        require_utc(datetime(2024, 1, 1))
    assert money('0.10005') == Decimal('0.1000')
    assert jsonable({'cash': money('12.3')}) == {'cash': '12.3000'}
    with pytest.raises(ValueError):
        money('NaN')
