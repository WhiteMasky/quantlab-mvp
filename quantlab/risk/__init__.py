"""Independent, fail-closed admission checks; never submits or manages orders.

An allowed decision is single-use for an intent/client ID, not a broker fill or a
cash reservation. The execution gateway must serialize check/submit/reconcile and
supply an accurate account, open-order count and reserved cash. Reserves are
conservatively charged to gross exposure and the proposed BUY's symbol. Pending
SELL quantities cannot be inferred from this interface and are not authorized.
Only SIMULATED is attestable through the current interface; IBKR_PAPER remains
blocked here pending an external environment-verification contract.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from zoneinfo import ZoneInfo

from quantlab.models import AccountSnapshot, OrderIntent, jsonable, money, require_utc

__all__ = ['RiskLimits', 'RiskDecision', 'RiskEngine']

ZERO = Decimal('0')
ONE = Decimal('1')
NY = ZoneInfo('America/New_York')
UTC = timezone.utc


def _decimal(value, *, positive=False) -> bool:
    return (isinstance(value, Decimal) and value.is_finite()
            and (value > ZERO if positive else value >= ZERO))


def _utc(value) -> bool:
    try:
        if not isinstance(value, datetime):
            return False
        require_utc(value)
        return True
    except (ValueError, TypeError, OverflowError):
        return False


@dataclass(frozen=True)
class RiskLimits:
    """Ratios are Decimal fractions; all currency amounts are USD.

    Fee/slippage reserves are deliberately hypothetical software-test budgets,
    NOT a broker tariff or a guarantee of the eventual market execution price.
    No configuration can enable borrowing or an unverified environment.
    """

    max_position_weight: Decimal = Decimal('0.40')
    max_position_notional: Decimal = Decimal('50000')
    max_order_notional: Decimal = Decimal('50000')
    max_gross_exposure: Decimal = Decimal('0.90')
    max_leverage: Decimal = Decimal('1')
    min_cash_buffer: Decimal = Decimal('0.05')
    max_daily_loss: Decimal = Decimal('0.03')
    max_drawdown: Decimal = Decimal('0.20')
    max_open_orders: int = 20
    max_orders_per_minute: int = 30
    max_quote_age_seconds: int = 60
    max_account_age_seconds: int = 300
    max_price_deviation: Decimal = Decimal('0.03')
    fee_per_share: Decimal = Decimal('0.005')
    minimum_fee: Decimal = Decimal('1')
    execution_buffer: Decimal = Decimal('0.001')

    def __post_init__(self):
        for name in ('max_position_weight', 'max_gross_exposure', 'max_leverage',
                     'max_daily_loss', 'max_drawdown'):
            value = getattr(self, name)
            if not _decimal(value, positive=True) or value > ONE:
                raise ValueError(f'{name} must be a Decimal in (0, 1]')
        for name in ('min_cash_buffer', 'max_price_deviation', 'execution_buffer'):
            value = getattr(self, name)
            if not _decimal(value) or value >= ONE:
                raise ValueError(f'{name} must be a Decimal in [0, 1)')
        for name in ('max_position_notional', 'max_order_notional'):
            if not _decimal(getattr(self, name), positive=True):
                raise ValueError(f'{name} must be a positive finite Decimal')
        for name in ('fee_per_share', 'minimum_fee'):
            if not _decimal(getattr(self, name)):
                raise ValueError(f'{name} must be a nonnegative finite Decimal')
        if self.minimum_fee == ZERO and self.fee_per_share == ZERO:
            raise ValueError('An explicit positive fee budget is required')
        for name in ('max_open_orders', 'max_orders_per_minute',
                     'max_quote_age_seconds', 'max_account_age_seconds'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be a positive integer')


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


@lru_cache(maxsize=8)
def _calendar(year: int):
    # Import failure/calendar coverage uncertainty must never become weekdays-only
    # authorization. Use real XNYS holidays, exceptional closures, DST and half days.
    import exchange_calendars
    return exchange_calendars.get_calendar(
        'XNYS', start=f'{year}-01-01', end=f'{year}-12-31')


def _session_times(now: datetime):
    day = now.astimezone(NY).date()
    calendar = _calendar(day.year)
    if not calendar.is_session(day.isoformat()):
        return None
    return (calendar.session_open(day.isoformat()).to_pydatetime(),
            calendar.session_close(day.isoformat()).to_pydatetime())


class RiskEngine:
    """SQLite-backed check/audit service. Use one database per account/timeline.

    Tables are module-namespaced and unrelated tables are never removed. A single
    connection keeps :memory: state alive. BEGIN IMMEDIATE serializes independent
    instances as well as threads. Failed storage cannot return an allowed check.
    Rejected *unchanged* intents may be retried; approved IDs cannot be replayed.
    Daily loss uses the worst observed equity in the NY trading date (latched until
    date rollover); drawdown uses a durable lifetime high-water mark. Clearing a
    manual halt does not erase loss or rate protections.
    """

    def __init__(self, db_path, limits=None):
        self.limits = limits if limits is not None else RiskLimits()
        if not isinstance(self.limits, RiskLimits):
            raise TypeError('limits must be RiskLimits')
        self._lock = RLock()
        # Do not reinterpret :memory: as a Path or create parent directories.
        self._db = sqlite3.connect(str(db_path) if isinstance(db_path, Path) else db_path,
                                   timeout=10, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA busy_timeout = 10000')
        with self._transaction():
            self._db.execute('''CREATE TABLE IF NOT EXISTS quantlab_risk_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload TEXT NOT NULL)''')
            self._db.execute('''CREATE TABLE IF NOT EXISTS quantlab_risk_intents (
                intent_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL, approved INTEGER NOT NULL,
                first_seen TEXT NOT NULL, approved_at TEXT)''')
            self._db.execute('''CREATE TABLE IF NOT EXISTS quantlab_risk_rate (
                intent_id TEXT PRIMARY KEY, at TEXT NOT NULL)''')
            self._db.execute('''CREATE TABLE IF NOT EXISTS quantlab_risk_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                event TEXT NOT NULL, intent_id TEXT, symbol TEXT,
                allowed INTEGER, reasons TEXT NOT NULL, details TEXT NOT NULL)''')
            self._db.execute('''INSERT OR IGNORE INTO quantlab_risk_state
                (singleton, payload) VALUES (1, ?)''', (json.dumps({
                    'halted': False, 'halt_reason': '', 'day': None,
                    'day_start_equity': None, 'day_low_equity': None,
                    'peak_equity': None, 'last_equity': None,
                    'last_checked_at': None, 'last_account_at': None,
                }),))
            self._db.execute('CREATE TABLE IF NOT EXISTS quantlab_risk_config (singleton INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            stored = self._db.execute('SELECT payload FROM quantlab_risk_config WHERE singleton=1').fetchone()
            if stored and limits is None:
                values = json.loads(stored['payload'])
                self.limits = RiskLimits(**{name: Decimal(value) if isinstance(value, str) else value for name, value in values.items()})
            encoded = json.dumps(jsonable(self.limits), sort_keys=True)
            if stored is None or stored['payload'] != encoded:
                self._db.execute('INSERT INTO quantlab_risk_config VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload', (encoded,))
                self._audit(datetime.now(UTC), 'limits_configured', details={'limits': jsonable(self.limits)})

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise

    def close(self):
        with self._lock:
            self._db.close()

    def _state(self):
        row = self._db.execute(
            'SELECT payload FROM quantlab_risk_state WHERE singleton = 1').fetchone()
        if row is None:
            raise ValueError('Missing risk state')
        state = json.loads(row['payload'])
        if type(state['halted']) is not bool or not isinstance(state['halt_reason'], str):
            raise ValueError('Invalid halt state')
        for name in ('day_start_equity', 'day_low_equity', 'peak_equity', 'last_equity'):
            if state[name] is not None:
                state[name] = Decimal(state[name])
                if not _decimal(state[name], positive=True):
                    raise ValueError('Invalid persisted equity')
        if state['last_checked_at'] is not None:
            if not _utc(datetime.fromisoformat(state['last_checked_at'])):
                raise ValueError('Invalid persisted timestamp')
        return state

    def _save(self, state):
        self._db.execute('UPDATE quantlab_risk_state SET payload = ? WHERE singleton = 1',
                         (json.dumps(jsonable(state), sort_keys=True),))

    def _audit(self, at, event, *, intent_id=None, symbol=None, allowed=None,
               reasons=None, details=None):
        self._db.execute('''INSERT INTO quantlab_risk_audit
            (at, event, intent_id, symbol, allowed, reasons, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
                         (at.isoformat(), event, intent_id, symbol, allowed,
                          json.dumps(reasons or []), json.dumps(jsonable(details or {}))))

    def set_halt(self, halted: bool, reason: str):
        if type(halted) is not bool or not isinstance(reason, str) or not reason.strip():
            raise ValueError('Halt requires a bool and a nonempty reason')
        with self._transaction():
            state = self._state()
            state['halted'], state['halt_reason'] = halted, reason.strip()
            self._save(state)
            self._audit(datetime.now(UTC), 'halt', details={
                'halted': halted, 'reason': reason.strip(),
                'cancels_orders': False, 'liquidates_positions': False})

    def _validate(self, intent, account, quote_price, quote_at, now,
                  open_orders, reserved_cash, market_open):
        reasons = []
        now_ok = _utc(now)
        if not now_ok:
            reasons.append('now_not_utc')
        intent_ok = isinstance(intent, OrderIntent)
        if intent_ok:
            intent_ok = (all(isinstance(v, str) and v.strip() for v in (
                intent.intent_id, intent.client_order_id, intent.symbol))
                and intent.side in ('BUY', 'SELL') and type(intent.quantity) is int
                and intent.quantity > 0 and _decimal(intent.reference_price, positive=True)
                and intent.tif in ('DAY', 'GTC') and _utc(intent.created_at))
        if not intent_ok:
            reasons.append('invalid_intent')
        else:
            if intent.mode == 'IBKR_PAPER':
                reasons.append('environment_unverified')
            elif intent.mode != 'SIMULATED':
                reasons.append('unsupported_environment')
            if now_ok and intent.created_at > now:
                reasons.append('future_intent')
        quote_ok = _decimal(quote_price, positive=True)
        if not quote_ok:
            reasons.append('invalid_quote_price')
        if not _utc(quote_at):
            reasons.append('quote_not_utc')
        elif now_ok:
            age = (now - quote_at).total_seconds()
            if age < 0:
                reasons.append('future_quote')
            elif age > self.limits.max_quote_age_seconds:
                reasons.append('stale_quote')
        if quote_ok and intent_ok:
            if abs(quote_price / intent.reference_price - ONE) > self.limits.max_price_deviation:
                reasons.append('price_deviation')
        if market_open is not True:
            reasons.append('market_closed' if market_open is False else 'market_state_unknown')
        if now_ok:
            try:
                times = _session_times(now)
                if times is None or not times[0] <= now < times[1]:
                    reasons.append('outside_market_session')
            except Exception:
                reasons.append('market_calendar_unavailable')
        if type(open_orders) is not int or open_orders < 0:
            reasons.append('invalid_open_orders')
        elif open_orders >= self.limits.max_open_orders:
            reasons.append('pending_order_limit')
        reserves_ok = _decimal(reserved_cash)
        if not reserves_ok:
            reasons.append('invalid_reserved_cash')
        elif type(open_orders) is int and open_orders > 0:
            if reserved_cash == ZERO:
                reasons.append('pending_reservations_unknown')
            if intent_ok and intent.side == 'SELL':
                reasons.append('pending_sell_quantity_unknown')
        account_ok = isinstance(account, AccountSnapshot)
        if account_ok and account.reconciled is not True:
            reasons.append('account_unreconciled')
            account_ok = False
        if isinstance(account, AccountSnapshot):
            if not _utc(account.as_of):
                reasons.append('account_not_utc')
                account_ok = False
            elif now_ok:
                age = (now - account.as_of).total_seconds()
                if age < 0:
                    reasons.append('future_account')
                    account_ok = False
                elif (age > self.limits.max_account_age_seconds
                      or account.as_of.astimezone(NY).date() != now.astimezone(NY).date()):
                    reasons.append('stale_account')
                    account_ok = False
            if not (_decimal(account.cash) and _decimal(account.equity, positive=True)
                    and _decimal(account.day_start_equity, positive=True)
                    and _decimal(account.peak_equity, positive=True)
                    and isinstance(account.positions, dict)):
                account_ok = False
            if account_ok:
                gross = ZERO
                for symbol, pos in account.positions.items():
                    if not (isinstance(symbol, str) and symbol.strip()
                            and getattr(pos, 'symbol', None) == symbol
                            and type(getattr(pos, 'quantity', None)) is int
                            and pos.quantity >= 0
                            and _decimal(getattr(pos, 'market_price', None), positive=True)
                            and _decimal(getattr(pos, 'average_cost', None))):
                        account_ok = False
                        break
                    gross += money(pos.quantity * pos.market_price)
                if account_ok:
                    tolerance = Decimal('0.0001') * (len(account.positions) + 1)
                    if abs(account.equity - account.cash - gross) > tolerance:
                        reasons.append('account_equity_mismatch')
                        account_ok = False
        if not account_ok:
            reasons.append('invalid_account')
        return reasons, intent_ok, account_ok, quote_ok, reserves_ok, now_ok

    def _observe(self, state, account, now):
        day = now.astimezone(NY).date().isoformat()
        if state['day'] is None or day > state['day']:
            state['day'] = day
            state['day_start_equity'] = money(account.day_start_equity)
            state['day_low_equity'] = money(account.equity)
        elif day < state['day']:
            raise ValueError('Regressive risk trading day')
        else:
            # A smaller caller-supplied baseline must not erase a previous loss.
            state['day_start_equity'] = max(state['day_start_equity'], money(account.day_start_equity))
            state['day_low_equity'] = min(state['day_low_equity'], money(account.equity))
        state['peak_equity'] = max(state['peak_equity'] or ZERO,
                                   money(account.peak_equity), money(account.equity))
        state['last_equity'] = money(account.equity)
        state['last_account_at'] = account.as_of.isoformat()

    @staticmethod
    def _losses(state):
        daily = drawdown = ZERO
        if state['day_start_equity'] is not None:
            daily = max(ZERO, ONE - state['day_low_equity'] / state['day_start_equity'])
        if state['peak_equity'] is not None:
            drawdown = max(ZERO, ONE - state['last_equity'] / state['peak_equity'])
        return daily, drawdown

    def _budget(self, intent, account, quote_price, reserved_cash):
        limits = self.limits
        reasons = []
        held = account.positions.get(intent.symbol)
        quantity = held.quantity if held else 0
        gross = sum((money(p.quantity * (quote_price if s == intent.symbol else p.market_price))
                     for s, p in account.positions.items()), ZERO)
        equity = min(account.equity, account.cash + gross)
        fee = money(max(limits.minimum_fee, limits.fee_per_share * intent.quantity))
        mark = money(quote_price * intent.quantity)
        if intent.side == 'BUY':
            budget_price = max(quote_price, intent.reference_price) * (ONE + limits.execution_buffer)
            cost = money(budget_price * intent.quantity)
            cash = money(account.cash - reserved_cash - cost - fee)
            equity = money(equity - fee - max(ZERO, cost - mark))
            post_gross = gross + mark + reserved_cash
            symbol_value = money((quantity + intent.quantity) * quote_price) + reserved_cash
            notional = cost
        else:
            if intent.quantity > quantity:
                reasons.append('insufficient_shares')
            proceeds = money(min(quote_price, intent.reference_price)
                             * (ONE - limits.execution_buffer) * intent.quantity)
            cash = money(account.cash - reserved_cash + proceeds - fee)
            equity = money(equity - fee - max(ZERO, mark - proceeds))
            post_gross = gross - mark + reserved_cash
            symbol_value = money(max(0, quantity - intent.quantity) * quote_price)
            notional = money(max(quote_price, intent.reference_price) * intent.quantity)
        if notional > limits.max_order_notional:
            reasons.append('order_notional_limit')
        if reserved_cash > account.cash:
            reasons.append('reserved_cash_exceeds_cash')
        if cash < ZERO:
            reasons.append('insufficient_cash')
        if equity <= ZERO:
            reasons.append('nonpositive_projected_equity')
        else:
            if cash < equity * limits.min_cash_buffer:
                reasons.append('cash_buffer_limit')
            if post_gross > equity * limits.max_leverage or account.cash < ZERO:
                reasons.append('leverage_limit')
            if post_gross > equity * limits.max_gross_exposure:
                reasons.append('gross_exposure_limit')
            if symbol_value > equity * limits.max_position_weight:
                reasons.append('position_weight_limit')
        if symbol_value > limits.max_position_notional:
            reasons.append('position_notional_limit')
        return reasons, {'order_notional': money(notional), 'fee_budget': fee,
                         'projected_cash_after_reserves': cash,
                         'projected_equity': equity, 'projected_gross': money(post_gross)}

    def check(self, intent, account, quote_price, quote_at, now, open_orders=0,
              reserved_cash=Decimal('0'), market_open=True) -> RiskDecision:
        """Validate and durably record only; never call a broker or mutate inputs.

        All checks run even on duplicates, halts and low-cash accounts, so stale
        quotes and new hazards are not hidden by a cached authorization. Rate is
        the number of newly approved unique intents in (now - 60 seconds, now].
        Rejected attempts are audited but do not consume authorization capacity.
        """
        try:
            with self._transaction():
                state = self._state()
                reasons, intent_ok, account_ok, quote_ok, reserves_ok, now_ok = self._validate(
                    intent, account, quote_price, quote_at, now, open_orders, reserved_cash, market_open)
                if state['halted']:
                    reasons.append('halted')
                chronological = now_ok
                if now_ok and state['last_checked_at'] is not None:
                    if now < datetime.fromisoformat(state['last_checked_at']):
                        chronological = False
                        reasons.append('time_regression')
                if account_ok and state['last_account_at'] is not None:
                    if account.as_of < datetime.fromisoformat(state['last_account_at']):
                        account_ok = False
                        reasons.append('account_time_regression')
                if chronological and account_ok:
                    self._observe(state, account, now)
                daily, drawdown = self._losses(state)
                if daily >= self.limits.max_daily_loss:
                    reasons.append('daily_loss_limit')
                if drawdown >= self.limits.max_drawdown:
                    reasons.append('drawdown_limit')
                details = {'daily_loss': daily, 'drawdown': drawdown}
                if intent_ok and account_ok and quote_ok and reserves_ok:
                    budget_reasons, budget = self._budget(intent, account, quote_price, reserved_cash)
                    reasons.extend(budget_reasons)
                    details.update(budget)
                prior = None
                fingerprint = None
                conflict = False
                if intent_ok:
                    fingerprint = hashlib.sha256(json.dumps(
                        jsonable(intent), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    rows = self._db.execute('''SELECT * FROM quantlab_risk_intents
                        WHERE intent_id = ? OR client_order_id = ?''',
                                            (intent.intent_id, intent.client_order_id)).fetchall()
                    if rows:
                        prior = rows[0]
                        conflict = (len(rows) != 1 or prior['fingerprint'] != fingerprint
                                    or prior['intent_id'] != intent.intent_id
                                    or prior['client_order_id'] != intent.client_order_id)
                        if conflict:
                            reasons.append('intent_id_conflict')
                        elif prior['approved']:
                            reasons.append('duplicate_intent')
                if chronological:
                    threshold = (now - timedelta(seconds=60)).isoformat()
                    self._db.execute('DELETE FROM quantlab_risk_rate WHERE at <= ?', (threshold,))
                    count = self._db.execute('SELECT COUNT(*) FROM quantlab_risk_rate').fetchone()[0]
                    if count >= self.limits.max_orders_per_minute:
                        reasons.append('order_rate_limit')
                    state['last_checked_at'] = now.isoformat()
                decision = RiskDecision(not reasons, list(dict.fromkeys(reasons)))
                event_time = now if now_ok else datetime.now(UTC)
                if intent_ok and chronological and not conflict:
                    if prior is None:
                        self._db.execute('''INSERT INTO quantlab_risk_intents
                            (intent_id, client_order_id, fingerprint, approved, first_seen, approved_at)
                            VALUES (?, ?, ?, ?, ?, ?)''',
                                         (intent.intent_id, intent.client_order_id, fingerprint,
                                          int(decision.allowed), event_time.isoformat(),
                                          event_time.isoformat() if decision.allowed else None))
                    elif decision.allowed:
                        self._db.execute('''UPDATE quantlab_risk_intents
                            SET approved = 1, approved_at = ? WHERE intent_id = ?''',
                                         (event_time.isoformat(), intent.intent_id))
                    if decision.allowed:
                        self._db.execute('INSERT INTO quantlab_risk_rate (intent_id, at) VALUES (?, ?)',
                                         (intent.intent_id, event_time.isoformat()))
                self._save(state)
                self._audit(event_time, 'check', intent_id=intent.intent_id if intent_ok else None,
                            symbol=intent.symbol if intent_ok else None, allowed=decision.allowed,
                            reasons=decision.reasons, details=details)
                return decision
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError,
                DecimalException, OverflowError):
            # There can be no durable success without the state/audit transaction.
            # If storage itself is unavailable, this refusal cannot promise an audit.
            return RiskDecision(False, ['risk_state_or_input_unavailable'])

    def status(self) -> dict:
        try:
            with self._lock:
                state = self._state()
                daily, drawdown = self._losses(state)
                count = self._db.execute('SELECT COUNT(*) FROM quantlab_risk_rate').fetchone()[0]
                return {**jsonable(state), 'daily_loss': str(daily), 'drawdown': str(drawdown),
                        'daily_loss_breached': daily >= self.limits.max_daily_loss,
                        'drawdown_breached': drawdown >= self.limits.max_drawdown,
                        'orders_in_window': count, 'rate_as_of': state['last_checked_at'],
                        'limits': jsonable(self.limits), 'storage_available': True,
                        'supported_environment': 'SIMULATED',
                        'fee_assumption': 'Hypothetical fee and execution-price reserves; not a broker tariff.',
                        'limitations': [
                            'Check is not execution or reservation; serialize admission and submission.',
                            'Caller must supply accurate reconciled account and pending cash reservations.',
                            'Rate window is as of the last explicit check time, not wall-clock time.',
                            'IBKR_PAPER cannot be independently verified through this interface.',
                            'Other positions use snapshot marks; per-symbol quote timestamps are unavailable.',
                            'Risk state belongs to one account/timeline; use separate databases for backtests.',
                        ]}
        except (sqlite3.Error, ValueError, KeyError, TypeError, DecimalException):
            return {'halted': True, 'halt_reason': 'risk_state_unavailable',
                    'storage_available': False}

    def audit(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute('SELECT * FROM quantlab_risk_audit ORDER BY id').fetchall()
            return [{**dict(row), 'allowed': None if row['allowed'] is None else bool(row['allowed']),
                     'reasons': json.loads(row['reasons']), 'details': json.loads(row['details'])}
                    for row in rows]
