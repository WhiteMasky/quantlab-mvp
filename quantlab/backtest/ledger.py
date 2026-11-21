"""Whole-share, long-only cash ledger; no broker or adjusted-price accounting."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR

from quantlab.models import AccountSnapshot, CorporateAction, Fill, Position, money, require_utc

ZERO = Decimal('0')


def _decimal(value, name: str, *, positive: bool = False) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f'{name} must be finite and {"positive" if positive else "nonnegative"}')
    return result


class Ledger:
    """In-memory accounting with atomic, idempotent fills and defensive snapshots.

    ``apply_fill`` returns False for an identical fill_id replay and raises for
    conflicting replays, insufficient cash, short sales or invalid input. Buy
    average cost includes commissions. Money is USD, rounded half-even to 1e-4.
    Times must be UTC and mutations chronological. ``snapshot`` never lends out
    mutable internal positions. This ledger is not a durable settlement engine.

    CorporateAction has no pay-date, action ID or fractional-share terms. Cash
    dividends therefore credit on ex-date, per post-split entitled share. Splits
    keep whole shares and approximate fractional cash-in-lieu at raw ex-date
    opening price, immediately. No adjusted_close enters the ledger. Exact
    duplicate economic actions are deduplicated; revisions need upstream IDs.
    """

    def __init__(self, initial_cash: Decimal = Decimal('100000'), as_of: datetime | None = None):
        self._cash = money(_decimal(initial_cash, 'initial_cash'))
        self._as_of = as_of or datetime(1970, 1, 1, tzinfo=timezone.utc)
        require_utc(self._as_of)
        self._positions: dict[str, Position] = {}
        self._fills: dict[str, Fill] = {}
        self._actions: set[tuple] = set()
        self._day_start_equity = self._cash
        self._peak_equity = self._cash
        self._session = None
        self.total_commissions = ZERO
        self.dividend_cash = ZERO
        self.cash_in_lieu = ZERO

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def positions(self) -> dict[str, Position]:
        return {symbol: replace(position) for symbol, position in self._positions.items()}

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills.values())

    def _check_time(self, at: datetime) -> None:
        require_utc(at)
        if at < self._as_of:
            raise ValueError('Ledger mutations must be chronological')

    def _equity(self) -> Decimal:
        return money(self._cash + sum((p.market_price * p.quantity for p in self._positions.values()), ZERO))

    def _advance(self, at: datetime) -> None:
        self._as_of = at
        self._peak_equity = max(self._peak_equity, self._equity())

    def snapshot(self, as_of: datetime | None = None) -> AccountSnapshot:
        at = as_of or self._as_of
        self._check_time(at)
        return AccountSnapshot(
            cash=self._cash, equity=self._equity(), positions=self.positions,
            as_of=at, reconciled=True, day_start_equity=self._day_start_equity,
            peak_equity=self._peak_equity,
        )

    def start_session(self, at: datetime) -> None:
        """Set the daily-loss baseline before applying opening marks/actions."""
        from zoneinfo import ZoneInfo
        self._check_time(at)
        session = at.astimezone(ZoneInfo('America/New_York')).date()
        if session != self._session:
            self._day_start_equity = self._equity()
            self._session = session
        self._advance(at)

    def mark_to_market(self, prices: dict[str, Decimal], at: datetime) -> AccountSnapshot:
        self._check_time(at)
        checked = {symbol: money(_decimal(price, 'market_price', positive=True)) for symbol, price in prices.items()}
        if any(price <= 0 for price in checked.values()):
            raise ValueError('Rounded market price must be positive')
        for symbol, price in checked.items():
            if symbol in self._positions:
                self._positions[symbol].market_price = price
        self._advance(at)
        return self.snapshot()

    def apply_fill(self, fill: Fill) -> bool:
        if not fill.fill_id or not fill.order_id or not fill.symbol:
            raise ValueError('Fill, order and symbol identifiers are required')
        previous = self._fills.get(fill.fill_id)
        if previous is not None:
            if previous != fill:
                raise ValueError('Conflicting replay for fill_id')
            return False
        self._check_time(fill.at)
        if fill.side not in ('BUY', 'SELL') or type(fill.quantity) is not int or fill.quantity <= 0:
            raise ValueError('Positive whole-share BUY/SELL fills only')
        price = money(_decimal(fill.price, 'fill price', positive=True))
        fee = money(_decimal(fill.commission, 'commission'))
        if price <= 0:
            raise ValueError('Rounded fill price must be positive')
        old = self._positions.get(fill.symbol)
        old_quantity = old.quantity if old else 0
        notional = money(price * fill.quantity)
        if fill.side == 'BUY':
            cash = money(self._cash - notional - fee)
            quantity = old_quantity + fill.quantity
            basis = (old.average_cost * old_quantity if old else ZERO) + notional + fee
            position = Position(fill.symbol, quantity, money(basis / quantity), price)
        else:
            if fill.quantity > old_quantity:
                raise ValueError('Short sales are not permitted')
            cash = money(self._cash + notional - fee)
            quantity = old_quantity - fill.quantity
            position = Position(fill.symbol, quantity, old.average_cost, price)
        if cash < 0:
            raise ValueError('Fill would create negative cash')
        # All validation precedes mutation: rejected fills are safe to retry.
        self._cash = cash
        if quantity:
            self._positions[fill.symbol] = position
        else:
            self._positions.pop(fill.symbol, None)
        self._fills[fill.fill_id] = fill
        self.total_commissions = money(self.total_commissions + fee)
        self._advance(fill.at)
        return True

    def apply_corporate_action(self, action: CorporateAction, at: datetime, raw_price: Decimal) -> bool:
        """Apply once to pre-open holdings; see class docstring for approximations.

        The caller schedules this at the ex-date open BEFORE any fills. A late
        known_at or a wrong ex-date is rejected, never silently backdated.
        """
        from zoneinfo import ZoneInfo
        require_utc(action.known_at)
        ratio = _decimal(action.split_ratio, 'split_ratio', positive=True)
        dividend = _decimal(action.cash_dividend, 'cash_dividend')
        key = (action.symbol, action.ex_date, ratio, dividend)
        if key in self._actions:
            return False
        self._check_time(at)
        if action.known_at > at:
            raise ValueError('Corporate action is not yet known')
        if at.astimezone(ZoneInfo('America/New_York')).date() != action.ex_date:
            raise ValueError('Corporate actions require their ex-date session')
        price = money(_decimal(raw_price, 'raw_price', positive=True))
        if price <= 0 or not action.symbol:
            raise ValueError('Positive raw price and symbol required')
        old = self._positions.get(action.symbol)
        if old:
            entitlement = old.quantity
            exact_shares = Decimal(entitlement) * ratio
            whole_shares = int(exact_shares.to_integral_value(rounding=ROUND_FLOOR))
            fractional_cash = money((exact_shares - whole_shares) * price)
            dividend_cash = money(exact_shares * dividend)
            if whole_shares:
                self._positions[action.symbol] = Position(
                    action.symbol, whole_shares, money(old.average_cost / ratio), price,
                )
            else:
                del self._positions[action.symbol]
            self._cash = money(self._cash + fractional_cash + dividend_cash)
            self.dividend_cash = money(self.dividend_cash + dividend_cash)
            self.cash_in_lieu = money(self.cash_in_lieu + fractional_cash)
        self._actions.add(key)
        self._advance(at)
        return True
