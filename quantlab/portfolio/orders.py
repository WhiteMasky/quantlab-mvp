"""Pure order planning. Execution, durable deduplication and fill checks live elsewhere."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, localcontext

from quantlab.models import AccountSnapshot, OrderIntent, TargetWeight, money, require_utc

from .allocation import _decimal, _symbol, _unit

_ZERO = Decimal('0')
_ONE = Decimal('1')


def _fee(quantity: int, per_share: Decimal, minimum: Decimal) -> Decimal:
    return money(max(minimum, per_share * quantity)) if quantity else money(0)


def _intent(key: str, symbol: str, side: str, quantity: int, price: Decimal, at: datetime) -> OrderIntent:
    # Do NOT use Python hash(), mutable quantity/price, or a wall-clock timestamp.
    # The caller's decision key owns the cycle; symbol+side owns a leg in it.
    payload = json.dumps(['quantlab.portfolio.v1', key, symbol, side], ensure_ascii=True, separators=(',', ':'))
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return OrderIntent(
        intent_id=f'portfolio-{digest}',
        client_order_id=f'ql-{digest[:32]}',
        symbol=symbol,
        side=side,
        quantity=quantity,
        created_at=at,
        reference_price=price,
    )


def rebalance_orders(
    targets: list[TargetWeight],
    account: AccountSnapshot,
    prices: dict[str, Decimal],
    as_of: datetime,
    rebalance_key,
    *,
    cash_buffer=Decimal('0.10'),
    fee_per_share=Decimal('0.005'),
    minimum_fee=Decimal('1'),
    execution_buffer_bps=Decimal('6'),
) -> list[OrderIntent]:
    """Plan deterministic whole-share sells before cash-limited buys.

    Missing target symbols are targeted at zero, including an empty target list.
    Gross target weights must sum to <= 1; if necessary they are proportionally
    reduced to respect cash_buffer. Quantities are floored at reference prices.
    Buys reserve adverse execution prices, commissions, and an equity-relative
    cash buffer. Execution prices are rounded half-even to USD 0.0001 before
    multiplying by shares, matching the shared monetary/execution convention.
    Defaults are ILLUSTRATIVE TEST assumptions (0.005/share, USD 1
    minimum, 6 bps adverse execution), not a broker tariff or guaranteed bound.
    Parameters must match or exceed downstream execution assumptions.

    Sale proceeds are projected at adverse prices, net of fees. Buying against
    them assumes earlier sells COMPLETE. Executors MUST check actual available
    cash/independent risk and pending reservations before each order; partial or
    refused sells do not authorize borrowing. This function never sends orders.
    Tiny sales whose fees exceed proceeds plus available cash are omitted.

    rebalance_key must be a nonempty, durable decision identity, e.g.
    '2026-11-21:trend:data-version:account'. Reuse it for retries and give a new
    key to each new decision/session/account. Same key/symbol/side has the SAME
    IDs even if retry time, quantity or price changes: broker persistence must
    deduplicate/refuse changed retries rather than execute them again. A new
    key produces distinct IDs; this module keeps no in-memory dedup state.

    Only known account timestamps are accepted. Plain Decimal prices carry no
    timestamps: quote availability/freshness is a required caller/risk check.
    Inputs are never mutated; reference price is never an adjusted close.
    """
    require_utc(as_of)
    require_utc(account.as_of)
    if account.as_of > as_of:
        raise ValueError('account snapshot is not yet available at as_of')
    if not account.reconciled:
        raise ValueError('account must be reconciled before planning orders')
    if not isinstance(rebalance_key, str) or not rebalance_key.strip():
        raise ValueError('rebalance_key must be a nonempty durable decision key')
    buffer = _unit(cash_buffer, 'cash_buffer')
    per_share = _decimal(fee_per_share, 'fee_per_share')
    minimum = _decimal(minimum_fee, 'minimum_fee')
    bps = _decimal(execution_buffer_bps, 'execution_buffer_bps')
    if per_share < 0 or minimum < 0 or not _ZERO <= bps < Decimal('10000'):
        raise ValueError('fees must be nonnegative; execution_buffer_bps must be in [0, 10000)')
    cash = _decimal(account.cash, 'account cash')
    equity = _decimal(account.equity, 'account equity')
    if cash < 0 or equity < 0 or cash > equity:
        raise ValueError('long-only account requires 0 <= cash <= equity')
    quantities = {}
    for symbol, position in account.positions.items():
        _symbol(symbol)
        if position.symbol != symbol:
            raise ValueError('position key does not match its symbol')
        if type(position.quantity) is not int or position.quantity < 0:
            raise ValueError('positions must contain nonnegative whole shares')
        if position.quantity:
            quantities[symbol] = position.quantity
    weights = {}
    for target in targets:
        symbol = _symbol(target.symbol)
        weight = _unit(target.weight, 'target weight')
        if symbol in weights and weights[symbol] != weight:
            raise ValueError(f'Conflicting duplicate targets for {symbol}')
        weights[symbol] = weight
    with localcontext() as context:
        context.prec = 50
        total_weight = sum(weights.values(), _ZERO)
        if total_weight > _ONE:
            raise ValueError('target weights cannot sum to more than 1')
        if equity == 0 and quantities:
            raise ValueError('positive positions require positive account equity')
        scale = min(_ONE, (_ONE - buffer) / total_weight) if total_weight else _ONE
        needed = set(quantities) | {symbol for symbol, weight in weights.items() if weight > 0}
        quotes = {}
        for symbol in sorted(needed):
            if symbol not in prices:
                raise ValueError(f'Missing executable price for {symbol}')
            price = _decimal(prices[symbol], f'price for {symbol}')
            if price <= 0:
                raise ValueError(f'Price for {symbol} must be positive')
            quotes[symbol] = price
        desired = {
            symbol: int((equity * weights.get(symbol, _ZERO) * scale / quotes[symbol]).to_integral_value(rounding=ROUND_FLOOR))
            for symbol in needed
        }
        adverse = bps / Decimal('10000')
        reserve = money(equity * buffer)
        projected_cash = money(cash)
        orders = []
        sells = []
        for symbol in sorted(needed):
            quantity = quantities.get(symbol, 0) - desired[symbol]
            if quantity > 0:
                execution_price = money(quotes[symbol] * (_ONE - adverse))
                proceeds = money(quantity * execution_price) - _fee(quantity, per_share, minimum)
                sells.append((symbol, quantity, proceeds))
        # Cash-generating sales precede fee-dominated sales, then lexical ties.
        for symbol, quantity, proceeds in sorted(sells, key=lambda item: (item[2] < 0, item[0])):
            if projected_cash + proceeds < 0:
                continue
            orders.append(_intent(rebalance_key, symbol, 'SELL', quantity, quotes[symbol], as_of))
            projected_cash += proceeds

        for symbol in sorted(needed):
            requested = desired[symbol] - quantities.get(symbol, 0)
            if requested <= 0:
                continue
            available = max(_ZERO, projected_cash - reserve)
            execution_price = money(quotes[symbol] * (_ONE + adverse))

            def cost(quantity: int) -> Decimal:
                return money(quantity * execution_price) + _fee(quantity, per_share, minimum)

            # Monotonic integer search includes the minimum-fee discontinuity.
            low, high = 0, requested
            while low < high:
                mid = (low + high + 1) // 2
                if cost(mid) <= available:
                    low = mid
                else:
                    high = mid - 1
            if low:
                orders.append(_intent(rebalance_key, symbol, 'BUY', low, quotes[symbol], as_of))
                projected_cash -= cost(low)
        return orders
