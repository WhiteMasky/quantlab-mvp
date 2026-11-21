"""Durable offline order simulator. No sockets, implicit fills, or liquidation."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3
import threading
import uuid

from quantlab.models import AccountSnapshot, Fill, Order, OrderIntent, Position, jsonable, money, require_utc

TERMINAL = frozenset({'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED'})
STATUSES = TERMINAL | {'PENDING', 'SUBMITTED', 'PARTIALLY_FILLED', 'PENDING_CANCEL', 'UNKNOWN'}


class BrokerSafetyError(RuntimeError):
    """Operation refused; do not automatically retry using a new intent ID."""


class Journal:
    def __init__(self, db_path):
        # Parent creation belongs to the caller; never silently choose another database.
        self.db_path = str(db_path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS broker_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS broker_orders (
                order_id TEXT PRIMARY KEY, intent_id TEXT UNIQUE NOT NULL,
                client_order_id TEXT UNIQUE NOT NULL, intent TEXT NOT NULL,
                status TEXT NOT NULL, filled INTEGER NOT NULL DEFAULT 0,
                reported INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                sequence INTEGER NOT NULL DEFAULT -1, cancelled_at TEXT,
                broker_id INTEGER UNIQUE);
            CREATE TABLE IF NOT EXISTS broker_fills (
                fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES broker_orders(order_id),
                payload TEXT NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM broker_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT INTO broker_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, json.dumps(value, sort_keys=True)))

    def close(self):
        with self.lock:
            self.db.close()


def _intent(payload):
    data = json.loads(payload)
    data['created_at'] = datetime.fromisoformat(data['created_at'])
    data['reference_price'] = Decimal(data['reference_price'])
    return OrderIntent(**data)


def _order(row):
    if row is None:
        raise KeyError('Unknown order ID')
    return Order(row['order_id'], _intent(row['intent']), row['status'], row['filled'],
                 row['reason'], row['broker_id'])


def _fill(payload):
    data = json.loads(payload)
    data['at'] = datetime.fromisoformat(data['at'])
    data['price'] = Decimal(data['price'])
    data['commission'] = Decimal(data['commission'])
    return Fill(**data)


def _account_positions(account):
    """Validate a caller's snapshot without treating it as external proof."""
    issues, quantities = [], {}
    if not isinstance(account, AccountSnapshot):
        return ['INVALID_ACCOUNT'], quantities
    try:
        require_utc(account.as_of)
    except (ValueError, AttributeError, TypeError):
        issues.append('INVALID_ACCOUNT_TIMESTAMP')
    if account.reconciled is not True or any(
        not isinstance(v, Decimal) or not v.is_finite() or v < 0
        for v in (account.cash, account.equity)
    ):
        issues.append('INVALID_OR_UNRECONCILED_ACCOUNT')
    if not isinstance(account.positions, dict):
        return issues + ['INVALID_POSITION'], quantities
    for symbol, position in account.positions.items():
        if (not isinstance(symbol, str) or not symbol.strip()
                or not isinstance(position, Position) or position.symbol != symbol
                or type(position.quantity) is not int or position.quantity < 0
                or any(not isinstance(v, Decimal) or not v.is_finite() or v < 0
                       for v in (position.average_cost, position.market_price))):
            if 'INVALID_POSITION' not in issues:
                issues.append('INVALID_POSITION')
        elif position.quantity:
            quantities[symbol] = position.quantity
    return issues, quantities


class SimulatedBroker:
    def __init__(self, db_path, *, baseline_positions=None):
        """Opening whole-share quantities, fixed before the journal's first order.

        Omission means an empty opening portfolio, NOT adoption of the first
        reconcile(account) snapshot. A restart uses the stored baseline.
        """
        if baseline_positions is not None:
            if not isinstance(baseline_positions, dict) or any(
                not isinstance(s, str) or not s.strip() or type(q) is not int or q < 0
                for s, q in baseline_positions.items()
            ):
                raise ValueError('Baseline requires nonnegative whole-share symbol quantities')
            baseline_positions = {s: q for s, q in baseline_positions.items() if q}
        self._journal = Journal(db_path)
        self._connected = True
        self._submit_fault = None
        self._cancel_fault = None
        try:
            with self._journal.transaction() as db:
                initialized = self._journal.meta('sim_initialized', False)
                stored = self._journal.meta('sim_baseline_positions', {})
                if initialized and baseline_positions is not None and baseline_positions != stored:
                    raise BrokerSafetyError('Existing journal baseline cannot be changed')
                self._journal.set_meta('sim_baseline_positions', stored if initialized else (baseline_positions or {}))
                pending = db.execute("SELECT 1 FROM broker_orders WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','EXPIRED') OR reported != filled LIMIT 1").fetchone()
                if not initialized:
                    self._journal.set_meta('sim_reconciled', True)
                    self._journal.set_meta('sim_initialized', True)
                elif pending:
                    self._journal.set_meta('sim_reconciled', False)
        except Exception:
            self._journal.close()
            raise

    def _expected_positions(self):
        quantities = dict(self._journal.meta('sim_baseline_positions', {}))
        for fill in self.fills():
            quantities[fill.symbol] = quantities.get(fill.symbol, 0) + (
                fill.quantity if fill.side == 'BUY' else -fill.quantity)
        return {s: q for s, q in sorted(quantities.items()) if q}

    def _row(self, order_id):
        row = self._journal.db.execute('SELECT * FROM broker_orders WHERE order_id=?', (order_id,)).fetchone()
        if row is None:
            raise KeyError('Unknown order ID')
        return row

    def inject_submit_fault(self, fault):
        """Offline fault injection for the next NEW intent, never a duplicate."""
        if fault not in ('timeout', 'reject', 'disconnect'):
            raise ValueError('Unsupported simulated fault')
        self._submit_fault = fault

    def inject_cancel_timeout(self):
        self._cancel_fault = 'timeout'

    def submit(self, intent: OrderIntent) -> Order:
        if intent.mode != 'SIMULATED':
            raise BrokerSafetyError('Simulator accepts SIMULATED intents only')
        if not intent.intent_id or not intent.client_order_id or not intent.symbol:
            raise ValueError('Nonempty intent, client and symbol IDs required')
        with self._journal.transaction() as db:
            rows = db.execute('SELECT * FROM broker_orders WHERE intent_id=? OR client_order_id=?',
                              (intent.intent_id, intent.client_order_id)).fetchall()
            if rows:
                if len(rows) != 1 or _intent(rows[0]['intent']) != intent:
                    raise BrokerSafetyError('ID reuse with different intent payload')
                return _order(rows[0])  # UNKNOWN is deliberately not retransmitted.
            if not self._connected or not self._journal.meta('sim_reconciled', False):
                raise BrokerSafetyError('Disconnected or reconciliation required')
            fault, self._submit_fault = self._submit_fault, None
            status, reason = 'SUBMITTED', ''
            if fault == 'reject':
                status, reason = 'REJECTED', 'Injected simulator rejection'
            elif fault in ('timeout', 'disconnect'):
                status, reason = 'UNKNOWN', 'Submission outcome unknown; reconcile, never blindly resend'
                self._journal.set_meta('sim_reconciled', False)
                if fault == 'disconnect':
                    self._connected = False
            order_id = 'sim-' + hashlib.sha256(intent.intent_id.encode()).hexdigest()[:32]
            db.execute('INSERT INTO broker_orders(order_id,intent_id,client_order_id,intent,status,reason) VALUES (?,?,?,?,?,?)',
                       (order_id, intent.intent_id, intent.client_order_id, json.dumps(jsonable(intent), sort_keys=True), status, reason))
            return _order(self._row(order_id))

    def fill(self, order_id, quantity, price, commission, at, fill_id=None) -> Fill | None:
        require_utc(at)
        if type(quantity) is not int or quantity <= 0:
            raise ValueError('Fill quantity must be positive whole shares')
        if not isinstance(price, Decimal) or not isinstance(commission, Decimal):
            raise ValueError('Decimal price and commission required')
        if not price.is_finite() or price <= 0 or not commission.is_finite() or commission < 0:
            raise ValueError('Invalid fill price or commission')
        price, commission = money(price), money(commission)
        if price <= 0:
            raise ValueError('Fill price rounds to zero')
        if fill_id is None:
            fill_id = 'sim-fill-' + uuid.uuid4().hex
        if not isinstance(fill_id, str) or not fill_id:
            raise ValueError('Nonempty fill ID required')
        with self._journal.transaction() as db:
            row = self._row(order_id)
            intent = _intent(row['intent'])
            fill = Fill(fill_id, order_id, intent.symbol, intent.side, quantity, price, commission, at)
            previous = db.execute('SELECT payload FROM broker_fills WHERE fill_id=?', (fill_id,)).fetchone()
            if previous:
                if _fill(previous[0]) != fill:
                    raise BrokerSafetyError('Conflicting duplicate execution ID')
                return None
            if at < intent.created_at:
                raise ValueError('Execution precedes order creation')
            if row['status'] in ('REJECTED', 'EXPIRED'):
                raise BrokerSafetyError('Execution conflicts with terminal order')
            if row['cancelled_at'] and at > datetime.fromisoformat(row['cancelled_at']):
                raise BrokerSafetyError('Execution is after confirmed simulator cancellation')
            total = row['filled'] + quantity
            if total > intent.quantity:
                raise ValueError('Execution overfills order')
            # Late callbacks are valid even while disconnected; they are evidence, not sends.
            status = 'FILLED' if total == intent.quantity else 'PARTIALLY_FILLED'
            if row['status'] in ('CANCELLED', 'PENDING_CANCEL') and total < intent.quantity:
                status = row['status']
            db.execute('INSERT INTO broker_fills VALUES (?,?,?)',
                       (fill_id, order_id, json.dumps(jsonable(fill), sort_keys=True)))
            db.execute('UPDATE broker_orders SET filled=?,reported=MAX(reported,?),status=? WHERE order_id=?',
                       (total, total, status, order_id))
            if any(q < 0 for q in self._expected_positions().values()):
                self._journal.set_meta('sim_reconciled', False)
            return fill

    def on_order_status(self, order_id, status, filled_quantity=None, sequence=None, at=None, reason='') -> Order:
        """Inject exchange evidence. Status callbacks NEVER fabricate Fill records.

        Optional sequence is an offline simulator sequence, not an IBKR guarantee.
        Cumulative execution quantity cannot regress; an ahead-of-executions status
        keeps recovery blocked until matching executions have arrived.
        """
        if status not in STATUSES:
            raise ValueError('Unsupported status')
        if at is not None:
            require_utc(at)
        if sequence is not None and (type(sequence) is not int or sequence < 0):
            raise ValueError('Invalid callback sequence')
        with self._journal.transaction() as db:
            row = self._row(order_id)
            order = _order(row)
            reported = row['reported'] if filled_quantity is None else filled_quantity
            if type(reported) is not int or not 0 <= reported <= order.intent.quantity:
                raise ValueError('Invalid cumulative quantity')
            if sequence is not None and sequence <= row['sequence']:
                return order
            if reported < max(row['filled'], row['reported']):
                return order
            if row['status'] in TERMINAL and status != row['status']:
                return order
            if status == 'FILLED' and reported != order.intent.quantity:
                raise ValueError('Filled status requires full cumulative quantity')
            if at is not None and at < order.intent.created_at:
                raise ValueError('Status callback precedes order creation')
            if reported > row['filled'] or status in ('UNKNOWN', 'PENDING_CANCEL'):
                self._journal.set_meta('sim_reconciled', False)
            effective = status
            if status == 'FILLED' and row['filled'] != order.intent.quantity:
                effective = 'UNKNOWN'
            elif row['status'] == 'PENDING_CANCEL' and status in ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED'):
                effective = 'PENDING_CANCEL'
            elif status in ('PENDING', 'SUBMITTED') and row['filled']:
                effective = 'PARTIALLY_FILLED'
            elif row['status'] in ('SUBMITTED', 'PARTIALLY_FILLED') and status == 'PENDING':
                effective = row['status']
            cancel_at = row['cancelled_at']
            if status == 'CANCELLED' and cancel_at is None:
                cancel_at = (at or datetime.now(timezone.utc)).isoformat()
            db.execute('UPDATE broker_orders SET status=?,reported=?,sequence=?,reason=?,cancelled_at=? WHERE order_id=?',
                       (effective, reported, sequence if sequence is not None else row['sequence'], reason, cancel_at, order_id))
            return _order(self._row(order_id))

    def cancel(self, order_id, authorized=False) -> Order:
        if authorized is not True:
            raise BrokerSafetyError('Explicit cancellation authorization required')
        with self._journal.transaction() as db:
            row = self._row(order_id)
            if row['status'] in TERMINAL:
                return _order(row)
            if not self._connected:
                raise BrokerSafetyError('Disconnected; cancellation not confirmed')
            if row['status'] == 'UNKNOWN':
                raise BrokerSafetyError('Unknown submission must be resolved before cancellation')
            fault, self._cancel_fault = self._cancel_fault, None
            if fault:
                db.execute("UPDATE broker_orders SET status='PENDING_CANCEL',reason='Cancel timeout; outcome unknown' WHERE order_id=?", (order_id,))
                self._journal.set_meta('sim_reconciled', False)
            elif row['status'] != 'PENDING_CANCEL':
                db.execute("UPDATE broker_orders SET status='CANCELLED',cancelled_at=? WHERE order_id=?",
                           (datetime.now(timezone.utc).isoformat(), order_id))
            return _order(self._row(order_id))

    def orders(self) -> list[Order]:
        with self._journal.lock:
            return [_order(row) for row in self._journal.db.execute('SELECT * FROM broker_orders ORDER BY rowid')]

    def fills(self) -> list[Fill]:
        with self._journal.lock:
            return [_fill(row[0]) for row in self._journal.db.execute('SELECT payload FROM broker_fills ORDER BY rowid')]

    def disconnect(self):
        with self._journal.transaction():
            self._connected = False
            self._journal.set_meta('sim_reconciled', False)

    def reconnect(self):
        with self._journal.transaction():
            self._connected = True
            self._journal.set_meta('sim_reconciled', False)
        return self.status()

    def reconcile(self, account: AccountSnapshot | None = None) -> dict:
        issues = []
        with self._journal.transaction() as db:
            if not self._connected:
                issues.append('DISCONNECTED')
            rows = db.execute('SELECT * FROM broker_orders').fetchall()
            unresolved = [r['order_id'] for r in rows if r['status'] in ('UNKNOWN', 'PENDING_CANCEL') or r['reported'] != r['filled']]
            if unresolved:
                issues.append('UNRESOLVED_ORDER_EVIDENCE')
            expected = self._expected_positions()
            if any(q < 0 for q in expected.values()):
                issues.append('NEGATIVE_DERIVED_POSITION')
            mismatches = {}
            if account is None and self._journal.meta('sim_account_check_required', False):
                issues.append('ACCOUNT_RECHECK_REQUIRED')
            if account is not None:
                account_issues, actual = _account_positions(account)
                issues.extend(account_issues)
                if not account_issues:
                    if any(f.at > account.as_of for f in self.fills()):
                        issues.append('ACCOUNT_PRECEDES_LOCAL_FILLS')
                    mismatches = {
                        s: {'expected': expected.get(s, 0), 'actual': actual.get(s, 0)}
                        for s in sorted(expected.keys() | actual.keys())
                        if expected.get(s, 0) != actual.get(s, 0)
                    }
                    if mismatches:
                        issues.append('POSITION_MISMATCH')
                # An omitted snapshot must not erase a previously failed account
                # comparison, including after restart. Require corrected evidence.
                self._journal.set_meta('sim_account_check_required', bool(
                    account_issues or mismatches or 'ACCOUNT_PRECEDES_LOCAL_FILLS' in issues))
            self._journal.set_meta('sim_reconciled', not issues)
        return {'reconciled': not issues, 'issues': issues, 'unresolved_order_ids': unresolved,
                'expected_positions': expected, 'position_mismatches': mismatches,
                'account_checked': account is not None,
                'scope': 'offline baseline plus local fills; no external account or cash-ledger proof',
                'orders': len(self.orders()), 'fills': len(self.fills())}

    def status(self) -> dict:
        with self._journal.lock:
            orders = self.orders()
            reconciled = self._connected and self._journal.meta('sim_reconciled', False)
            return {'environment': 'SIMULATED', 'live_enabled': False, 'connected': self._connected,
                    'reconciled': reconciled, 'can_submit': reconciled, 'halted': not reconciled,
                    'account_check_required': self._journal.meta('sim_account_check_required', False),
                    'unknown_orders': sum(o.status == 'UNKNOWN' for o in orders),
                    'open_orders': sum(o.status not in TERMINAL for o in orders),
                    'integration_verified': False, 'broker': 'offline simulator'}

    def close(self):
        self._journal.close()
