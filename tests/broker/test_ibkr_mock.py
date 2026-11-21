"""MOCK ONLY: no IBKR account, official binary, socket, or paper integration.

The fake transport and fake version/runtime exercise local safety logic only.
"""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from quantlab.broker import BrokerSafetyError, IBKRAdapter, IBKRConfig
from quantlab.broker import ibkr
from quantlab.models import AccountSnapshot

D = Decimal


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MockTransport:
    """In-memory transport spy. Its connect() does NOT open any socket."""
    def __init__(self, adapter, generation, *, handshake=True):
        self.adapter = adapter
        self.generation = generation
        self.handshake = handshake
        self.calls = []
        self.stopped = threading.Event()
        self.fail_on = None

    def emit(self, name, *args):
        self.adapter._dispatch(self.generation, name, args, {})

    def connect(self, host, port, clientId):
        self.calls.append(('connect', (host, port, clientId)))
        if self.handshake:
            self.emit('managedAccounts', self.adapter.config.account)
            self.emit('nextValidId', 40)

    def run(self):
        self.stopped.wait()

    def disconnect(self):
        self.calls.append(('disconnect', ()))
        self.stopped.set()

    def execution_filter(self):
        return SimpleNamespace(acctCode=self.adapter.config.account)

    def __getattr__(self, method):
        if method not in ibkr._QUERY_METHODS:
            raise AssertionError('Unexpected vendor mutation or method: ' + method)

        def query(*args):
            self.calls.append((method, args))
            if method == self.fail_on:
                raise TimeoutError('MOCK transport timeout')
        return query


def config(**changes):
    # Arbitrary test-only socket number, NOT a recommended TWS/Gateway port.
    return replace(IBKRConfig(port=12345, account='MOCK_ACCOUNT', client_id=17,
                              enabled=True, official_api_version='1051.1'), **changes)


def contract(**changes):
    return SimpleNamespace(**(dict(symbol='SPY', conId=123, secType='STK', currency='USD') | changes))


def execution(exec_id='mock.0001.01', **changes):
    return SimpleNamespace(**(dict(execId=exec_id, acctNumber='MOCK_ACCOUNT', orderId=45,
                                    clientId=17, permId=987, shares=D('3'), price=100.5,
                                    side='BOT', time='20261120 15:00:00 UTC',
                                    pendingPriceRevision=False) | changes))


def commission(exec_id='mock.0001.01', **changes):
    return SimpleNamespace(**(dict(execId=exec_id, commissionAndFees=1.0, currency='USD') | changes))


def complete(adapter):
    for tag, value in [('NetLiquidation', '100000'), ('TotalCashValue', '99000'),
                       ('AvailableFunds', '98000')]:
        adapter.accountSummary(adapter._summary_id, adapter.config.account, tag, value, 'USD')
    adapter.accountSummaryEnd(adapter._summary_id)
    adapter.positionEnd()
    adapter.openOrderEnd()
    adapter.execDetailsEnd(adapter._executions_id)
    adapter.currentTime(1795186800)


@pytest.fixture
def mock_adapter(tmp_path, monkeypatch):
    # Replacing this module's sys object avoids changing the interpreter itself.
    monkeypatch.setattr(ibkr, 'sys', SimpleNamespace(version_info=(3, 11, 0)))
    monkeypatch.setattr(ibkr, 'version', lambda name: '1051.1')
    monkeypatch.setattr(ibkr, '_make_transport', MockTransport)
    adapters = []

    def make(*, path=None, connect=True, **changes):
        adapter = IBKRAdapter(config(**changes), path or tmp_path / f'mock-{len(adapters)}.sqlite', clock=Clock())
        adapters.append(adapter)
        if connect:
            adapter.connect()
        return adapter

    yield make
    for adapter in reversed(adapters):
        adapter.close()


def test_mock_handshake_query_calls_and_permanent_mutation_block(mock_adapter):
    adapter = mock_adapter()
    methods = [name for name, _ in adapter._transport.calls]
    assert methods == ['connect', 'reqAccountSummary', 'reqPositions', 'reqAllOpenOrders',
                       'reqExecutions', 'reqCurrentTime']
    assert adapter._transport.calls[4][1][1].acctCode == 'MOCK_ACCOUNT'
    assert not adapter.status()['recovery_snapshot_complete']
    complete(adapter)
    state = adapter.status()
    assert state['recovery_snapshot_complete']
    assert state['environment'] == 'UNKNOWN'
    assert state['environment_verified'] is False
    assert state['can_submit'] is False
    assert state['reconciled'] is False
    assert state['live_enabled'] is False
    assert state['integration_verified'] is False
    for action in (lambda: adapter.submit(object()), lambda: adapter.cancel('45', authorized=True),
                   lambda: adapter._send('placeOrder', 45), lambda: adapter._send('reqGlobalCancel'),
                   lambda: adapter._send('cancelOrder', 45)):
        with pytest.raises(BrokerSafetyError):
            action()
    assert not any(name in ('placeOrder', 'cancelOrder', 'reqGlobalCancel')
                   for name, _ in adapter._transport.calls)


@pytest.mark.parametrize('account_name', ['DU_MOCK_PAPER', 'U_MOCK_LIVE', 'UNKNOWN_MOCK'])
def test_mock_account_prefix_and_requested_port_never_prove_environment(mock_adapter, account_name):
    adapter = mock_adapter(account=account_name)
    complete(adapter)
    assert adapter.status()['account_matched']
    assert not adapter.status()['environment_verified']
    assert not adapter.status()['can_submit']


@pytest.mark.parametrize('authorization', [False, None, 1, 'true'])
def test_mock_cancel_needs_authorization_before_environment_check(mock_adapter, authorization):
    adapter = mock_adapter()
    with pytest.raises(BrokerSafetyError, match='authorization'):
        adapter.cancel('45', authorized=authorization)


def test_actual_local_python310_refuses_connect_without_mock_transport(tmp_path):
    if sys.version_info >= (3, 11):
        pytest.skip('Actual-runtime check applies only to the offline Python 3.10 environment')
    adapter = IBKRAdapter(config(), tmp_path / 'actual-runtime.sqlite')
    try:
        with pytest.raises(BrokerSafetyError, match='Python >=3.11'):
            adapter.connect()
        assert adapter._transport is None
    finally:
        adapter.close()


def test_python310_real_connect_refused_before_loading_transport(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ibkr, 'sys', SimpleNamespace(version_info=(3, 10, 12)))
    monkeypatch.setattr(ibkr, '_make_transport', lambda *args: calls.append(args))
    adapter = IBKRAdapter(config(), tmp_path / 'python310.sqlite')
    try:
        with pytest.raises(BrokerSafetyError, match='Python >=3.11'):
            adapter.connect()
        assert calls == []
    finally:
        adapter.close()


@pytest.mark.parametrize('changes,message', [
    (dict(enabled=False), 'explicit enable'),
    (dict(official_api_version=None), 'Verify official'),
    (dict(official_api_version='1051.2'), 'version mismatch'),
])
def test_mock_connect_preflight_refuses_before_transport(mock_adapter, monkeypatch, changes, message):
    adapter = mock_adapter(connect=False, **changes)
    monkeypatch.setattr(ibkr, '_make_transport', lambda *args: pytest.fail('Preflight must not create transport'))
    with pytest.raises(BrokerSafetyError, match=message):
        adapter.connect()


def test_mock_missing_package_and_legacy_version_refused(mock_adapter, monkeypatch):
    adapter = mock_adapter(connect=False)

    def unavailable(name):
        raise LookupError('not installed')
    monkeypatch.setattr(ibkr, 'version', unavailable)
    with pytest.raises(BrokerSafetyError, match='official downloaded'):
        adapter.connect()
    monkeypatch.setattr(ibkr, 'version', lambda name: '9.81.1')
    adapter.config = config(official_api_version='9.81.1')
    with pytest.raises(BrokerSafetyError, match='legacy'):
        adapter.connect()
    assert adapter._transport is None


@pytest.mark.parametrize('changes', [
    dict(port=None), dict(port=0), dict(port=True), dict(client_id=0), dict(client_id=True),
    dict(host='example.com'), dict(host='0.0.0.0'), dict(environment='LIVE'), dict(environment='UNKNOWN'),
    dict(account=''), dict(account='A,B'), dict(account=' A'), dict(enabled='true'),
    dict(timeout_seconds=float('nan')), dict(timeout_seconds='10'), dict(heartbeat_seconds=0),
    dict(max_requests_per_second=21), dict(official_api_version='unverified'),
])
def test_invalid_configuration_never_accepted(changes):
    with pytest.raises((ValueError, BrokerSafetyError)):
        config(**changes)


def test_mock_handshake_timeout_disconnects_without_queries(mock_adapter, monkeypatch):
    transports = []

    def create(adapter, generation):
        transport = MockTransport(adapter, generation, handshake=False)
        transports.append(transport)
        return transport
    monkeypatch.setattr(ibkr, '_make_transport', create)
    adapter = mock_adapter(connect=False, timeout_seconds=0.01)
    with pytest.raises(BrokerSafetyError, match='Handshake timeout'):
        adapter.connect()
    assert [m for m, _ in transports[0].calls] == ['connect', 'disconnect']
    assert not adapter.status()['connected']


def test_mock_recovery_requires_all_end_barriers_and_correlated_ids(mock_adapter):
    adapter = mock_adapter()
    complete(adapter)
    adapter._barriers.remove('executions')
    adapter.execDetailsEnd(adapter._executions_id + 100)
    assert not adapter.status()['recovery_snapshot_complete']
    adapter.execDetailsEnd(adapter._executions_id)
    assert adapter.status()['recovery_snapshot_complete']
    calls = list(adapter._transport.calls)
    adapter.accountSummaryEnd(adapter._summary_id)
    adapter.accountSummaryEnd(adapter._summary_id)
    assert adapter._transport.calls == calls  # Duplicate end cannot consume pacing or resend.
    with pytest.raises(BrokerSafetyError, match='One recovery generation'):
        adapter.request_recovery()


def test_mock_disconnect_reconnect_ignores_old_callbacks_and_old_reader(mock_adapter):
    adapter = mock_adapter()
    complete(adapter)
    old = adapter._transport
    old_ids = adapter._summary_id, adapter._executions_id
    adapter._clock.advance(2)
    adapter.reconnect()
    assert adapter._summary_id > old_ids[1]
    assert not adapter.status()['recovery_snapshot_complete']
    old.emit('accountSummaryEnd', old_ids[0])
    old.emit('positionEnd')
    old.emit('openOrderEnd')
    old.emit('execDetailsEnd', old_ids[1])
    old.emit('connectionClosed')
    old.emit('nextValidId', 9999)
    adapter._run(old, old.generation)
    assert adapter.status()['connected']
    assert adapter.status()['recovery_barriers'] == []
    assert adapter.status()['next_order_id_floor'] == 40
    complete(adapter)
    assert adapter.status()['recovery_snapshot_complete']


def test_mock_restart_ids_fills_and_missing_commission_stay_durable(mock_adapter, tmp_path):
    path = tmp_path / 'restart.sqlite'
    original = mock_adapter(path=path)
    original.execDetails(original._executions_id, contract(), execution(orderId=250))
    old_request = original._executions_id
    original.close()
    restarted = mock_adapter(path=path)
    assert restarted._summary_id > old_request
    assert restarted.status()['next_order_id_floor'] == 251
    assert len(restarted.fills()) == 1
    complete(restarted)  # Old execution is intentionally outside the new mock query window.
    assert restarted.status()['missing_commissions'] == ['mock.0001.01']
    assert not restarted.status()['recovery_snapshot_complete']
    restarted.commissionAndFeesReport(commission())
    assert restarted.status()['recovery_snapshot_complete']
    assert restarted.status()['execution_history_complete'] is False
    assert restarted.fills()[0]['applied_to_ledger'] is False


def test_mock_partial_duplicate_and_commission_before_execution(mock_adapter):
    adapter = mock_adapter()
    adapter.commissionAndFeesReport(commission('mock.0002.01'))
    adapter.execDetails(adapter._executions_id, contract(), execution('mock.0002.01', shares=D('2')))
    adapter.execDetails(adapter._executions_id, contract(), execution())
    adapter.execDetails(adapter._executions_id, contract(), execution())
    assert len(adapter.fills()) == 2
    complete(adapter)
    assert not adapter.status()['recovery_snapshot_complete']
    adapter.commissionAndFeesReport(commission())
    adapter.commissionAndFeesReport(commission())
    assert adapter.status()['recovery_snapshot_complete']
    assert sum(D(f['quantity']) for f in adapter.fills()) == D('5')


@pytest.mark.parametrize('correction,flag', [
    ('execution-duplicate', 'CONFLICTING_EXECUTION_ID'),
    ('execution-correction', 'EXECUTION_CORRECTION_REQUIRES_REVIEW'),
    ('commission-correction', 'COMMISSION_CORRECTION_REQUIRES_REVIEW'),
])
def test_mock_evidence_conflicts_persist_across_restart(mock_adapter, tmp_path, correction, flag):
    path = tmp_path / (correction + '.sqlite')
    adapter = mock_adapter(path=path)
    adapter.execDetails(adapter._executions_id, contract(), execution())
    adapter.commissionAndFeesReport(commission())
    if correction == 'execution-duplicate':
        adapter.execDetails(adapter._executions_id, contract(), execution(price=200))
    elif correction == 'execution-correction':
        adapter.execDetails(adapter._executions_id, contract(), execution('mock.0001.02'))
    else:
        adapter.commissionAndFeesReport(commission(commissionAndFees=2))
    complete(adapter)
    assert flag in adapter.status()['errors']
    assert not adapter.status()['recovery_snapshot_complete']
    adapter.close()
    restarted = mock_adapter(path=path)
    assert flag in restarted.status()['errors']
    assert not restarted.status()['reconciled']


def test_mock_status_callbacks_are_not_fills_and_do_not_regress(mock_adapter):
    adapter = mock_adapter()

    def status(state, filled, remaining):
        adapter.orderStatus(45, state, D(filled), D(remaining), 100, 987, 0, 100, 17, '', 0)
    status('Submitted', '3', '7')
    status('Submitted', '3', '7')
    status('Submitted', '0', '10')
    assert adapter.fills() == []
    status('PendingCancel', '3', '7')
    status('Submitted', '3', '7')
    status('Cancelled', '3', '7')
    status('Submitted', '3', '7')
    adapter.openOrder(45, contract(), SimpleNamespace(account='MOCK_ACCOUNT', clientId=17,
                      permId=987, totalQuantity=D('10'), action='BUY'), SimpleNamespace(status='Submitted'))
    last = adapter.orders()[0]['last_status_callback']
    assert last['status'] == 'Cancelled'
    assert last['filled'] == '3'
    assert adapter.status()['next_order_id_floor'] == 46


def test_mock_unrelated_account_and_request_callbacks_are_filtered(mock_adapter):
    adapter = mock_adapter()
    adapter.execDetails(adapter._executions_id, contract(), execution(acctNumber='OTHER_MOCK'))
    adapter.execDetails(adapter._executions_id + 10, contract(), execution())
    adapter.position('OTHER_MOCK', contract(), D('1'), 100)
    adapter.accountSummary(adapter._summary_id, 'OTHER_MOCK', 'NetLiquidation', '1', 'USD')
    assert adapter.positions() == []
    assert adapter.fills() == []
    assert adapter.account()['values'] == {}
    adapter.managedAccounts('OTHER_MOCK')
    assert 'ACCOUNT_MISMATCH' in adapter.status()['errors']


@pytest.mark.parametrize('changes', [dict(shares=D('0.5')), dict(side='UNKNOWN'),
                                     dict(pendingPriceRevision=True)])
def test_mock_unsupported_execution_stays_blocked(mock_adapter, changes):
    adapter = mock_adapter()
    adapter.execDetails(adapter._executions_id, contract(), execution(**changes))
    assert 'UNSUPPORTED_EXECUTION' in adapter.status()['errors']


def test_mock_non_usd_and_short_positions_block_snapshot(mock_adapter):
    adapter = mock_adapter()
    adapter.position('MOCK_ACCOUNT', contract(currency='EUR'), D('-2'), 100)
    adapter.accountSummary(adapter._summary_id, 'MOCK_ACCOUNT', 'AvailableFunds', '-1', 'USD')
    adapter.commissionAndFeesReport(commission(currency='EUR'))
    assert {'UNSUPPORTED_POSITION', 'NEGATIVE_ACCOUNT_VALUE', 'UNSUPPORTED_COMMISSION'} <= set(adapter.status()['errors'])


def test_mock_callback_validation_error_is_sanitized_and_durable(mock_adapter):
    adapter = mock_adapter()
    adapter._transport.emit('position', 'MOCK_ACCOUNT', contract(), 'SECRET_BAD_VALUE', 100)
    assert 'CALLBACK_VALIDATION_FAILED' in adapter.status()['errors']
    assert 'SECRET_BAD_VALUE' not in json.dumps(adapter.status())


@pytest.mark.parametrize('code,flag', [
    (1100, 'CONNECTIVITY_1100'), (1101, 'RECONNECT_AND_RECOVER_1101'),
    (1102, 'RECONNECT_AND_RECOVER_1102'), (100, 'BROKER_PACING_LIMIT'),
    (354, 'MARKET_DATA_UNAVAILABLE_354'), (201, 'ORDER_ERROR_REQUIRES_RECONCILIATION_201'),
    (202, 'ORDER_ERROR_REQUIRES_RECONCILIATION_202'), (999, 'BROKER_ERROR_999'),
])
def test_mock_official_error_signature_requires_recovery_no_resend(mock_adapter, code, flag):
    adapter = mock_adapter()
    complete(adapter)
    calls = list(adapter._transport.calls)
    adapter.error(45, 1795186800, code, 'SECRET_ACCOUNT_RAW_MESSAGE', 'SECRET_REJECTION_JSON')
    assert flag in adapter.status()['errors']
    assert not adapter.status()['recovery_snapshot_complete']
    assert adapter._transport.calls == calls
    records = json.dumps(adapter._records('error'))
    assert 'SECRET' not in records
    assert 'MOCK_ACCOUNT' not in repr(adapter.config)
    assert 'MOCK_ACCOUNT' not in json.dumps(adapter.status())


def test_mock_info_codes_heartbeat_and_recovery_timeout(mock_adapter):
    adapter = mock_adapter()
    adapter.error(-1, 1, 2104, 'Mock data farm OK')
    assert adapter.status()['errors'] == []
    before = list(adapter._transport.calls)
    adapter._clock.advance(11)
    state = adapter.status()
    assert {'HEARTBEAT_TIMEOUT', 'RECOVERY_TIMEOUT'} <= set(state['errors'])
    assert not state['connected']
    assert adapter._transport.calls == before
    adapter._transport.emit('nextValidId', 70)
    assert not adapter.status()['connected']  # Late ID cannot revive a failed session.


def test_mock_heartbeat_staleness_local_pacing_and_send_timeout(mock_adapter):
    adapter = mock_adapter()
    complete(adapter)
    adapter._clock.advance(26)
    assert 'HEARTBEAT_STALE' in adapter.status()['errors']

    paced = mock_adapter(max_requests_per_second=6)
    complete(paced)
    with pytest.raises(BrokerSafetyError, match='pacing'):
        paced.heartbeat()
    assert 'LOCAL_PACING_LIMIT' in paced.status()['errors']

    failed = mock_adapter()
    complete(failed)
    failed._transport.fail_on = 'reqCurrentTime'
    before = len(failed._transport.calls)
    with pytest.raises(BrokerSafetyError, match='transport failed'):
        failed.heartbeat()
    assert 'QUERY_SEND_OUTCOME_UNKNOWN' in failed.status()['errors']
    failed.heartbeat()
    assert len(failed._transport.calls) == before + 1  # Never blind-resend a timed-out query.


def test_mock_external_account_cannot_bypass_recovery_or_environment(mock_adapter):
    adapter = mock_adapter()
    snapshot = AccountSnapshot(D('1'), D('1'), {}, datetime.now(timezone.utc))
    assert not adapter.reconcile(snapshot)['reconciled']
    complete(adapter)
    assert not adapter.reconcile(snapshot)['reconciled']
    assert not adapter.status()['can_submit']


def test_mock_account_client_scope_cannot_reuse_journal(mock_adapter, tmp_path):
    path = tmp_path / 'scope.sqlite'
    original = mock_adapter(path=path)
    original.close()
    with pytest.raises(BrokerSafetyError, match='another account/client'):
        IBKRAdapter(config(account='OTHER_MOCK'), path)
    with pytest.raises(BrokerSafetyError, match='another account/client'):
        IBKRAdapter(config(client_id=18), path)


def test_mock_official_eclient_ewrapper_adapter_glue(tmp_path, monkeypatch):
    # These deliberately fake classes verify composition/forwarding, NOT official
    # ibapi ABI compatibility; no external package is imported or installed.
    class EWrapper:
        pass

    class EClient:
        def __init__(self, wrapper):
            self.wrapper = wrapper

    modules = {name: ModuleType(name) for name in ('ibapi', 'ibapi.client', 'ibapi.wrapper', 'ibapi.execution')}
    modules['ibapi.client'].EClient = EClient
    modules['ibapi.wrapper'].EWrapper = EWrapper
    modules['ibapi.execution'].ExecutionFilter = SimpleNamespace
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    adapter = IBKRAdapter(config(enabled=False), tmp_path / 'glue.sqlite')
    try:
        transport = ibkr._make_transport(adapter, adapter._generation)
        assert isinstance(transport, EClient) and isinstance(transport, EWrapper)
        assert transport.wrapper is transport
        assert transport.execution_filter().acctCode == 'MOCK_ACCOUNT'
        transport.nextValidId(82)
        assert adapter.status()['next_order_id_floor'] == 82
        transport.error(-1, 1, 1100, 'mock failure')
        assert 'CONNECTIVITY_1100' in adapter.status()['errors']
    finally:
        adapter.close()


def test_local_example_is_disabled_and_has_no_guessed_port():
    path = Path(__file__).resolve().parents[2] / 'config.ibkr.example.json'
    data = json.loads(path.read_text())
    assert data['enabled'] is False
    assert data['environment'] == 'PAPER'
    assert data['port'] is None
    assert data['account'] == ''
    assert data['official_api_version'] is None
    with pytest.raises(ValueError):
        IBKRConfig(**data)
