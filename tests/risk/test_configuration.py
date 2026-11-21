from decimal import Decimal

from quantlab.risk import RiskEngine, RiskLimits


def test_limits_are_restored_and_changes_audited(tmp_path):
    path = tmp_path / 'risk.sqlite'
    first = RiskEngine(path, RiskLimits(max_order_notional=Decimal('5000')))
    first.set_halt(True, 'manual stop')
    first.close()
    second = RiskEngine(path)
    assert second.limits.max_order_notional == Decimal('5000')
    assert second.status()['halted']
    assert len([a for a in second.audit() if a['event'] == 'limits_configured']) == 1
    second.close()
    third = RiskEngine(path, RiskLimits(max_order_notional=Decimal('4000')))
    assert third.status()['halted']
    assert len([a for a in third.audit() if a['event'] == 'limits_configured']) == 2
    third.close()
