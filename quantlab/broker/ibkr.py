"""Optional official TWS API, query-only until paper environment can be proven.

There is intentionally no placeOrder/cancelOrder escape hatch. Managed accounts,
DU prefixes, a requested port and a local PAPER setting are not environment proof.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from importlib.metadata import version
import json
import math
import sys
import threading
import time

from quantlab.models import money
from .simulator import BrokerSafetyError, Journal


@dataclass(frozen=True)
class IBKRConfig:
    port: int
    account: str = field(repr=False)
    client_id: int
    enabled: bool = False
    host: str = '127.0.0.1'
    environment: str = 'PAPER'
    official_api_version: str | None = None
    timeout_seconds: float = 10.0
    heartbeat_seconds: float = 15.0
    max_requests_per_second: int = 10

    def __post_init__(self):
        if self.host not in ('127.0.0.1', '::1'):
            raise ValueError('Only explicit loopback socket hosts are allowed')
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('Explicit TWS socket port required; no default')
        if type(self.client_id) is not int or self.client_id <= 0:
            raise ValueError('Use an explicit positive client ID; client 0 binding is disabled')
        if (not isinstance(self.account, str) or not self.account.strip()
                or self.account != self.account.strip() or ',' in self.account):
            raise ValueError('Exactly one explicit account is required')
        if self.environment != 'PAPER':
            raise BrokerSafetyError('Live and unknown configured environments are prohibited')
        if type(self.enabled) is not bool:
            raise ValueError('enabled must be a JSON boolean')
        if self.official_api_version is not None and (
            not isinstance(self.official_api_version, str)
            or not self.official_api_version or any(
                not part.isdigit() for part in self.official_api_version.split('.'))
        ):
            raise ValueError('official_api_version must be the locally verified numeric version or null')
        for interval in (self.timeout_seconds, self.heartbeat_seconds):
            if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
                raise ValueError('Timeout and heartbeat intervals must be finite and positive')
        if type(self.max_requests_per_second) is not int or not 1 <= self.max_requests_per_second <= 20:
            raise ValueError('Conservative local pacing limit must be 1..20 requests/second')


_QUERY_METHODS = frozenset({'reqAccountSummary', 'cancelAccountSummary', 'reqPositions',
                            'reqAllOpenOrders', 'reqExecutions', 'reqCurrentTime'})

_CALLBACKS = ('nextValidId', 'managedAccounts', 'currentTime', 'connectionClosed',
              'accountSummary', 'accountSummaryEnd', 'position', 'positionEnd',
              'openOrder', 'openOrderEnd', 'orderStatus', 'execDetails', 'execDetailsEnd',
              'commissionAndFeesReport', 'commissionReport', 'error')


def _make_transport(adapter, generation):
    # Optional imports only after runtime/configuration/version preflight.
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.execution import ExecutionFilter

    class Transport(EWrapper, EClient):
        def __init__(self):
            EWrapper.__init__(self)
            EClient.__init__(self, self)

        def execution_filter(self):
            result = ExecutionFilter()
            result.acctCode = adapter.config.account
            return result

    def callback(name):
        def forward(self, *args, **kwargs):
            adapter._dispatch(generation, name, args, kwargs)
        return forward

    for name in _CALLBACKS:
        setattr(Transport, name, callback(name))
    return Transport()


def _decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite broker value')
    return result


class IBKRAdapter:
    def __init__(self, config: IBKRConfig, db_path, *, clock=time.monotonic):
        self.config = config
        self._clock = clock
        self._journal = Journal(db_path)
        self._lock = threading.RLock()
        try:
            with self._journal.transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS ibkr_records(kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,key))')
                scope = hashlib.sha256(f'{config.account}:{config.client_id}'.encode()).hexdigest()
                previous = self._journal.meta('ibkr_scope')
                if previous is not None and previous != scope:
                    raise BrokerSafetyError('Journal belongs to another account/client scope')
                self._journal.set_meta('ibkr_scope', scope)
        except Exception:
            self._journal.close()
            raise
        self._transport = None
        self._generation = 0
        self._run_thread = None
        self._health_thread = None
        self._stop = threading.Event()
        self._handshake = threading.Event()
        self._managed = threading.Event()
        self._requests = deque()
        self._reset_session()

    def _reset_session(self):
        self._connected = False
        self._account_match = False
        self._recovery_started = None
        self._barriers = set()
        self._summary_id = None
        self._executions_id = None
        self._summary = {}
        self._positions = {}
        self._open_orders = {}
        self._seen_executions = set()
        self._errors = []
        self._last_heartbeat = None
        self._heartbeat_pending = None
        self._heartbeat_at = None
        self._handshake.clear()
        self._managed.clear()

    def _record(self, kind, key, payload):
        with self._journal.transaction() as db:
            db.execute('INSERT INTO ibkr_records VALUES (?,?,?) ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload',
                       (kind, str(key), json.dumps(payload, sort_keys=True)))

    def _records(self, kind):
        with self._journal.lock:
            return {r['key']: json.loads(r['payload']) for r in self._journal.db.execute('SELECT key,payload FROM ibkr_records WHERE kind=? ORDER BY key', (kind,))}

    def _fail(self, code, persistent=False):
        if code not in self._errors:
            self._errors.append(code)
        if persistent:
            with self._journal.transaction():
                flags = self._journal.meta('ibkr_integrity_flags', [])
                if code not in flags:
                    flags.append(code)
                self._journal.set_meta('ibkr_integrity_flags', flags)

    def _dispatch(self, generation, name, args, kwargs):
        with self._lock:
            if generation != self._generation:
                return  # Never accept callbacks from a previous socket into a new barrier.
            try:
                getattr(self, name)(*args, **kwargs)
            except Exception:
                # Do not log raw vendor messages, contracts or full account identifiers.
                self._fail('CALLBACK_VALIDATION_FAILED', persistent=True)

    def connect(self):
        if sys.version_info < (3, 11):
            raise BrokerSafetyError('Official current TWS API requires Python >=3.11; no socket attempted')
        if self.config.enabled is not True:
            raise BrokerSafetyError('Local explicit enable required; no automatic broker connection')
        if not self.config.official_api_version:
            raise BrokerSafetyError('Verify official downloaded API version locally before enabling queries')
        try:
            installed = version('ibapi')
        except Exception as exc:
            raise BrokerSafetyError('Install ibapi only from the official downloaded TWS API source') from exc
        if installed != self.config.official_api_version or int(installed.split('.')[0]) < 10:
            raise BrokerSafetyError('Official API version mismatch or unsupported legacy package')
        if self._transport is not None:
            raise BrokerSafetyError('Disconnect explicitly before reconnecting')
        self._reset_session()
        self._generation += 1
        self._stop = threading.Event()
        try:
            self._transport = _make_transport(self, self._generation)
            self._transport.connect(self.config.host, self.config.port, clientId=self.config.client_id)
            self._run_thread = threading.Thread(target=self._run, args=(self._transport, self._generation),
                                                daemon=True, name='ibkr-reader')
            self._run_thread.start()
            if not self._handshake.wait(self.config.timeout_seconds):
                raise BrokerSafetyError('Handshake timeout; no submissions permitted')
            if not self._managed.wait(self.config.timeout_seconds) or not self._account_match:
                raise BrokerSafetyError('Managed account does not match local configuration')
            self.request_recovery()
            self._health_thread = threading.Thread(target=self._supervise, args=(self._stop, self._generation),
                                                   daemon=True, name='ibkr-heartbeat')
            self._health_thread.start()
        except Exception:
            self.disconnect()
            raise
        return self.status()

    def _run(self, transport, generation):
        try:
            transport.run()
        except Exception:
            with self._lock:
                if generation == self._generation:
                    self._fail('READER_FAILED')
        finally:
            with self._lock:
                if generation == self._generation:
                    self._connected = False

    def _supervise(self, stop, generation):
        interval = min(self.config.heartbeat_seconds, self.config.timeout_seconds)
        while not stop.wait(interval):
            with self._lock:
                if generation != self._generation:
                    return
                self.check_health()
                if not self._connected or self._errors:
                    continue
                if self._heartbeat_pending is None:
                    try:
                        self.heartbeat()
                    except BrokerSafetyError:
                        self._fail('HEARTBEAT_SEND_FAILED')

    def _send(self, method, *args):
        if method not in _QUERY_METHODS:
            raise BrokerSafetyError('Only allowlisted read-only queries are supported; mutations prohibited')
        if not self._connected or self._transport is None:
            raise BrokerSafetyError('Handshake/connection required for query')
        now = self._clock()
        while self._requests and now - self._requests[0] >= 1:
            self._requests.popleft()
        if len(self._requests) >= self.config.max_requests_per_second:
            self._fail('LOCAL_PACING_LIMIT')
            raise BrokerSafetyError('Local pacing limit reached; query not sent')
        self._requests.append(now)
        try:
            getattr(self._transport, method)(*args)
        except Exception as exc:
            self._fail('QUERY_SEND_OUTCOME_UNKNOWN')
            raise BrokerSafetyError('Query transport failed; explicit recovery required') from exc

    def _allocate_request_ids(self):
        with self._journal.transaction():
            value = self._journal.meta('ibkr_next_request_id', 1000000)
            self._journal.set_meta('ibkr_next_request_id', value + 2)
        return value, value + 1

    def request_recovery(self):
        with self._lock:
            if not self._connected or not self._account_match:
                raise BrokerSafetyError('Matched managed account and handshake required')
            if self._recovery_started is not None:
                raise BrokerSafetyError('One recovery generation per socket; reconnect for another snapshot')
            self._summary_id, self._executions_id = self._allocate_request_ids()
            self._recovery_started = self._clock()
            self._send('reqAccountSummary', self._summary_id, 'All', 'NetLiquidation,TotalCashValue,AvailableFunds')
            self._send('reqPositions')
            self._send('reqAllOpenOrders')  # Never bind or take over orders as client 0.
            self._send('reqExecutions', self._executions_id, self._transport.execution_filter())
            self.heartbeat()
        return self.status()

    def heartbeat(self):
        with self._lock:
            self.check_health()
            if self._heartbeat_pending is None:
                self._heartbeat_pending = self._clock()
                self._send('reqCurrentTime')
        return self.status()

    def check_health(self):
        now = self._clock()
        if self._heartbeat_pending is not None and now - self._heartbeat_pending > self.config.timeout_seconds:
            self._fail('HEARTBEAT_TIMEOUT')
            self._connected = False
        if self._recovery_started is not None and self._barriers != {'account', 'positions', 'orders', 'executions'}:
            if now - self._recovery_started > self.config.timeout_seconds:
                self._fail('RECOVERY_TIMEOUT')
        if self._last_heartbeat is not None and now - self._last_heartbeat > self.config.heartbeat_seconds + self.config.timeout_seconds:
            self._fail('HEARTBEAT_STALE')

    def nextValidId(self, orderId):
        if type(orderId) is not int or orderId < 0:
            raise ValueError('Invalid next order ID')
        with self._journal.transaction():
            self._journal.set_meta('ibkr_order_id_floor', max(orderId, self._journal.meta('ibkr_order_id_floor', 0)))
        if not self._errors:
            self._connected = True
        self._handshake.set()

    def managedAccounts(self, accountsList):
        self._account_match = self.config.account in {s.strip() for s in accountsList.split(',')}
        if not self._account_match:
            self._fail('ACCOUNT_MISMATCH')
        self._managed.set()

    def currentTime(self, timestamp):
        if self._heartbeat_pending is None:
            return
        self._heartbeat_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        self._last_heartbeat = self._clock()
        self._heartbeat_pending = None

    def accountSummary(self, reqId, account, tag, value, currency):
        if reqId != self._summary_id or account != self.config.account:
            return
        if tag not in ('NetLiquidation', 'TotalCashValue', 'AvailableFunds'):
            return
        if currency != 'USD':
            self._fail('UNSUPPORTED_ACCOUNT_CURRENCY')
            return
        amount = _decimal(value)
        if amount < 0:
            self._fail('NEGATIVE_ACCOUNT_VALUE')
        self._summary[tag] = str(money(amount))

    def accountSummaryEnd(self, reqId):
        if (self._recovery_started is not None and reqId == self._summary_id
                and 'account' not in self._barriers):
            self._barriers.add('account')
            self._send('cancelAccountSummary', reqId)  # Do not accumulate the two allowed subscriptions.

    def position(self, account, contract, position, avgCost):
        if account != self.config.account or self._recovery_started is None:
            return
        quantity = _decimal(position)
        if quantity != quantity.to_integral_value() or quantity < 0 or contract.secType != 'STK' or contract.currency != 'USD':
            self._fail('UNSUPPORTED_POSITION')
        self._positions[str(contract.conId)] = {
            'symbol': contract.symbol, 'con_id': contract.conId, 'quantity': str(quantity),
            'average_cost': str(money(_decimal(avgCost))), 'currency': contract.currency,
            'security_type': contract.secType, 'market_price': None}

    def positionEnd(self):
        if self._recovery_started is not None:
            self._barriers.add('positions')

    def openOrder(self, orderId, contract, order, orderState):
        if order.account != self.config.account or self._recovery_started is None:
            return
        key = f'{order.clientId}:{orderId}'
        self._open_orders[key] = {
            'order_id': orderId, 'client_id': order.clientId, 'perm_id': order.permId,
            'symbol': contract.symbol, 'quantity': str(_decimal(order.totalQuantity)),
            'side': order.action, 'status': orderState.status}
        self._observe_id(orderId)
        self._record('open_order', key, self._open_orders[key])

    def _observe_id(self, order_id):
        with self._journal.transaction():
            self._journal.set_meta('ibkr_order_id_floor', max(order_id + 1, self._journal.meta('ibkr_order_id_floor', 0)))

    def openOrderEnd(self):
        if self._recovery_started is not None:
            self._barriers.add('orders')

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId,
                    parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        key = f'{clientId}:{orderId}'
        # Account is absent from this callback. Keep it untrusted until openOrder
        # or execDetails associates it with the configured account.
        total, left = _decimal(filled), _decimal(remaining)
        if total < 0 or left < 0:
            raise ValueError('Negative cumulative order quantity')
        previous = self._records('order_status').get(key)
        if previous:
            if total < Decimal(previous['filled']):
                return
            if previous['status'] in ('Filled', 'Cancelled', 'ApiCancelled'):
                if status != previous['status'] and total <= Decimal(previous['filled']):
                    return
            ranks = {'PendingSubmit': 0, 'PreSubmitted': 1, 'Submitted': 2, 'PendingCancel': 3}
            if total == Decimal(previous['filled']) and ranks.get(status, 4) < ranks.get(previous['status'], 0):
                return
        self._observe_id(orderId)
        self._record('order_status', key, {'order_id': orderId, 'client_id': clientId,
                     'status': status, 'filled': str(total), 'remaining': str(left), 'perm_id': permId})

    def execDetails(self, reqId, contract, execution):
        if execution.acctNumber != self.config.account:
            return
        if reqId not in (-1, self._executions_id):
            return
        shares, price = _decimal(execution.shares), _decimal(execution.price)
        if (shares <= 0 or shares != shares.to_integral_value() or price <= 0
                or contract.secType != 'STK' or contract.currency != 'USD'
                or execution.side not in ('BOT', 'SLD')
                or getattr(execution, 'pendingPriceRevision', False)):
            self._fail('UNSUPPORTED_EXECUTION', persistent=True)
        exec_id = execution.execId
        if not exec_id:
            raise ValueError('Missing execution ID')
        payload = {'exec_id': exec_id, 'order_id': execution.orderId, 'client_id': execution.clientId,
                   'perm_id': execution.permId, 'symbol': contract.symbol,
                   'quantity': str(shares), 'price': str(price), 'side': execution.side,
                   'broker_time': execution.time, 'currency': contract.currency,
                   'security_type': contract.secType}
        prior = self._records('execution')
        if exec_id in prior:
            if prior[exec_id] != payload:
                self._fail('CONFLICTING_EXECUTION_ID', persistent=True)
            self._seen_executions.add(exec_id)
            return
        # Corrections are replacement evidence, NOT an additional fill. Keep all
        # raw evidence and block reconciliation; no silent double counting.
        if '.' in exec_id and any(k.rsplit('.', 1)[0] == exec_id.rsplit('.', 1)[0] for k in prior):
            self._fail('EXECUTION_CORRECTION_REQUIRES_REVIEW', persistent=True)
        self._record('execution', exec_id, payload)
        self._seen_executions.add(exec_id)
        self._observe_id(execution.orderId)

    def execDetailsEnd(self, reqId):
        if self._recovery_started is not None and reqId == self._executions_id:
            self._barriers.add('executions')

    def commissionAndFeesReport(self, report):
        amount = _decimal(report.commissionAndFees)
        if not isinstance(report.execId, str) or not report.execId:
            raise ValueError('Missing commission execution ID')
        if amount < 0 or report.currency != 'USD':
            self._fail('UNSUPPORTED_COMMISSION', persistent=True)
        payload = {'commission': str(money(amount)), 'currency': report.currency}
        previous = self._records('commission').get(report.execId)
        if previous and previous != payload:
            self._fail('COMMISSION_CORRECTION_REQUIRES_REVIEW', persistent=True)
            return
        self._record('commission', report.execId, payload)

    def commissionReport(self, report):
        # Legacy callback tolerated for offline fixtures; current API uses the
        # commissionAndFeesReport name. A legacy package still fails connect().
        from types import SimpleNamespace
        self.commissionAndFeesReport(SimpleNamespace(execId=report.execId,
                                     commissionAndFees=report.commission, currency=report.currency))

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=''):
        # Current official signature includes errorTime. Raw text may contain
        # account identifiers, so only numeric codes and categories are retained.
        if errorCode in (2104, 2106, 2107, 2108, 2158):
            return
        if errorCode in (502, 504, 1100, 1300):
            self._connected = False
            self._fail(f'CONNECTIVITY_{errorCode}')
        elif errorCode in (1101, 1102):
            self._barriers.clear()
            self._fail(f'RECONNECT_AND_RECOVER_{errorCode}')
        elif errorCode == 100:
            self._fail('BROKER_PACING_LIMIT')
        elif errorCode in (354, 10167, 10168, 10197, 2103, 2105):
            self._fail(f'MARKET_DATA_UNAVAILABLE_{errorCode}')
        elif errorCode in (201, 202):
            # Do not infer fill/rejection solely from a possibly unrelated reqId.
            self._fail(f'ORDER_ERROR_REQUIRES_RECONCILIATION_{errorCode}')
        else:
            self._fail(f'BROKER_ERROR_{errorCode}')
        self._record('error', f'{errorTime}:{reqId}:{errorCode}',
                     {'request_id': reqId, 'code': errorCode, 'error_time': errorTime})

    def connectionClosed(self):
        self._connected = False
        self._barriers.clear()
        self._fail('CONNECTION_CLOSED')

    def account(self):
        with self._lock:
            return {'values': dict(self._summary), 'currency': 'USD',
                    'available_funds': self._summary.get('AvailableFunds'),
                    'snapshot_complete': 'account' in self._barriers,
                    'environment_verified': False}

    def positions(self):
        with self._lock:
            return [dict(v) for _, v in sorted(self._positions.items())]

    def orders(self):
        with self._lock:
            statuses = self._records('order_status')
            return [dict(v, last_status_callback=statuses.get(k)) for k, v in sorted(self._open_orders.items())]

    def fills(self):
        """Raw execution evidence, NOT shared-model ledger Fills or booked P&L."""
        with self._lock:
            commissions = self._records('commission')
            return [dict(v, fees=commissions.get(k), applied_to_ledger=False)
                    for k, v in self._records('execution').items()]

    def reconcile(self, account=None):
        # An external AccountSnapshot cannot certify a broker environment or
        # substitute for the completed callback barrier.
        if self._recovery_started is None:
            return self.request_recovery()
        return self.status()

    def status(self):
        with self._lock:
            self.check_health()
            with self._journal.lock:
                flags = self._journal.meta('ibkr_integrity_flags', [])
                id_floor = self._journal.meta('ibkr_order_id_floor', None)
            # Missing durable evidence must not disappear when a new socket's
            # limited history window no longer includes an older execution.
            missing_fees = sorted(self._records('execution').keys() - self._records('commission').keys())
            complete = (self._connected and self._account_match and not self._errors and not flags
                        and self._last_heartbeat is not None and not missing_fees
                        and self._barriers == {'account', 'positions', 'orders', 'executions'}
                        and set(self._summary) == {'NetLiquidation', 'TotalCashValue', 'AvailableFunds'})
            return {'environment': 'UNKNOWN', 'requested_environment': 'IBKR_PAPER',
                    'environment_verified': False, 'live_enabled': False, 'can_submit': False,
                    'connected': self._connected, 'account_matched': self._account_match,
                    'reconciled': False, 'recovery_snapshot_complete': bool(complete),
                    'recovery_barriers': sorted(self._barriers), 'missing_commissions': missing_fees,
                    'errors': sorted(set(self._errors + flags)), 'heartbeat_at': self._heartbeat_at,
                    'next_order_id_floor': id_floor, 'integration_verified': False,
                    'submission_block_reason': 'Paper environment cannot be reliably verified; all order mutations blocked',
                    'execution_history_complete': False}

    def submit(self, intent):
        raise BrokerSafetyError('Paper environment unverified; placeOrder is permanently blocked in this release')

    def cancel(self, order_id, authorized=False):
        if authorized is not True:
            raise BrokerSafetyError('Explicit cancellation authorization required')
        raise BrokerSafetyError('Paper environment unverified; all real order mutations, including cancel, are blocked')

    def disconnect(self):
        with self._lock:
            self._stop.set()
            self._generation += 1
            self._connected = False
            self._barriers.clear()
            transport, self._transport = self._transport, None
            threads = (self._run_thread, self._health_thread)
        try:
            if transport is not None:
                transport.disconnect()
        except Exception:
            with self._lock:
                self._fail('DISCONNECT_FAILED')
        finally:
            for thread in threads:
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=self.config.timeout_seconds)

    def reconnect(self):
        self.disconnect()
        return self.connect()  # Explicit call only; no automatic retry loop.

    def close(self):
        self.disconnect()
        self._journal.close()
