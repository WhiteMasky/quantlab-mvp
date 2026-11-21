from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from quantlab.models import BacktestConfig, BacktestResult, EquityPoint, Fill, Order, money
from quantlab.portfolio import rebalance_orders
from quantlab.risk import RiskEngine, RiskLimits
from .ledger import Ledger

__all__ = ['Ledger', 'run_backtest']


def run_backtest(dataset, target_fn, config=None):
    config = config or BacktestConfig()
    if not dataset.bars or not callable(target_fn):
        raise ValueError('Nonempty market data and a target callback are required')
    for name in ('initial_cash', 'fee_per_share', 'minimum_fee', 'spread_bps', 'slippage_bps', 'max_participation'):
        value = getattr(config, name)
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError(f'Invalid {name}')
    if config.initial_cash <= 0 or not 0 < config.max_participation <= 1:
        raise ValueError('Positive cash and participation in (0,1] required')
    if config.spread_bps / 2 + config.slippage_bps >= 10000:
        raise ValueError('Execution costs must be less than 100%')
    if config.order_tif not in ('DAY', 'GTC'):
        raise ValueError('Unsupported order TIF')
    if any(type(v) is not int or v < 1 for v in (config.max_order_age_sessions, config.rebalance_every)):
        raise ValueError('Session counts must be positive integers')
    by_day = defaultdict(dict)
    for bar in dataset.bars:
        if bar.symbol in by_day[bar.session]:
            raise ValueError('Duplicate symbol/session')
        if any(not p.is_finite() or p <= 0 for p in (bar.open, bar.high, bar.low, bar.close, bar.adjusted_close)):
            raise ValueError('Invalid market price')
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError('Inconsistent OHLC')
        if type(bar.volume) is not int or bar.volume < 0 or bar.available_at < bar.close_at:
            raise ValueError('Invalid market volume/availability')
        by_day[bar.session][bar.symbol] = bar
    days = sorted(by_day)
    actions = defaultdict(list)
    for action in dataset.actions:
        actions[action.ex_date].append(action)
    ledger = Ledger(config.initial_cash)
    orders, curve, pending = [], [], []
    fees_paid, ages = defaultdict(lambda: Decimal('0')), {}
    adverse = (config.spread_bps / 2 + config.slippage_bps) / Decimal('10000')
    limits = RiskLimits(fee_per_share=config.fee_per_share, minimum_fee=max(config.minimum_fee, Decimal('0.0001')),
                        execution_buffer=max(adverse, Decimal('0.001')))
    risk = RiskEngine(':memory:', limits)
    warnings = [config.fee_assumption, 'Daily bars: next-session raw opening execution; participation uses previous available volume, not actual opening liquidity.',
                'USD cash account approximation: settlement delays, interest, taxes and stock-specific exchange calendars are not modeled.',
                'Dividend cash credited on ex-date; fractional split cash-in-lieu approximated at ex-date open, not actual payment terms.']
    chronological_bars = sorted(dataset.bars, key=lambda b: (b.available_at, b.symbol))
    known, cursor = [], 0
    try:
        for day_index, day in enumerate(days):
            day_bars = by_day[day]
            opened = min(b.open_at for b in day_bars.values())
            closed = max(b.close_at for b in day_bars.values())
            ledger.start_session(opened)
            for action in actions[day]:
                if action.symbol not in day_bars:
                    raise ValueError('Missing raw opening price for corporate action')
                ledger.apply_corporate_action(action, opened, day_bars[action.symbol].open)
            ledger.mark_to_market({s: b.open for s, b in day_bars.items()}, opened)
            prior_volume = {}
            for bar in chronological_bars:
                if bar.available_at >= opened:
                    break
                prior_volume[bar.symbol] = bar.volume
            budgets = {s: int(Decimal(v) * config.max_participation) for s, v in prior_volume.items()}
            next_pending = []
            for order in sorted(pending, key=lambda o: (o.intent.side == 'BUY', o.intent.symbol)):
                intent = order.intent
                ages[order.order_id] += 1
                remaining = intent.quantity - order.filled_quantity
                bar = day_bars.get(intent.symbol)
                if ages[order.order_id] > config.max_order_age_sessions:
                    order.status, order.reason = 'EXPIRED', 'Maximum session age reached'
                    continue
                if bar is None or bar.open_at <= intent.created_at:
                    order.reason = 'No eligible later-session raw opening quote'
                else:
                    quantity = min(remaining, budgets.get(intent.symbol, 0))
                    if quantity:
                        slice_intent = replace(intent, intent_id=f'{intent.intent_id}:{day}', client_order_id=f'{intent.client_order_id}:{day}', quantity=quantity)
                        account = ledger.snapshot(opened)
                        decision = risk.check(slice_intent, account, bar.open, opened, opened)
                        if not decision.allowed:
                            order.status, order.reason = 'REJECTED', '; '.join(decision.reasons)
                            continue
                        price = money(bar.open * (1 + (adverse if intent.side == 'BUY' else -adverse)))
                        cumulative = order.filled_quantity + quantity
                        commission = money(max(config.minimum_fee, config.fee_per_share * cumulative) - fees_paid[order.order_id])
                        fill = Fill(f'{order.order_id}:{day}:fill', order.order_id, intent.symbol, intent.side, quantity, price, commission, opened)
                        try:
                            ledger.apply_fill(fill)
                        except ValueError as error:
                            order.status, order.reason = 'REJECTED', str(error)
                            continue
                        fees_paid[order.order_id] += commission
                        budgets[intent.symbol] -= quantity
                        order.filled_quantity = cumulative
                        order.status = 'FILLED' if cumulative == intent.quantity else 'PARTIALLY_FILLED'
                        order.reason = '' if order.status == 'FILLED' else 'Previous-volume participation limit'
                    else:
                        order.reason = 'No previous-known volume capacity'
                if order.status != 'FILLED':
                    if intent.tif == 'DAY':
                        order.status = 'EXPIRED'
                        order.reason = (order.reason + '; DAY remainder expired').strip('; ')
                    else:
                        next_pending.append(order)
            pending = next_pending
            account = ledger.mark_to_market({s: b.close for s, b in day_bars.items()}, closed)
            gross = money(account.equity - account.cash)
            curve.append(EquityPoint(closed, account.cash, account.equity, gross))
            decision_at = closed + timedelta(minutes=30)
            while cursor < len(chronological_bars) and chronological_bars[cursor].available_at <= decision_at:
                known.append(chronological_bars[cursor])
                cursor += 1
            if day_index == len(days) - 1 or day_index % config.rebalance_every or pending:
                continue
            targets = target_fn(list(known), decision_at, ledger.snapshot(decision_at))
            if targets is None:
                continue
            prices = {bar.symbol: bar.close for bar in sorted(known, key=lambda b: (b.session, b.symbol))}
            needed = set(account.positions) | {t.symbol for t in targets if t.weight > 0}
            if not needed.issubset(prices):
                raise ValueError('Target references unavailable raw prices')
            planned = rebalance_orders(targets, ledger.snapshot(decision_at), prices, decision_at,
                f'{dataset.version}:{day}', fee_per_share=config.fee_per_share,
                minimum_fee=config.minimum_fee, execution_buffer_bps=adverse * 10000)
            for intent in planned:
                intent = replace(intent, tif=config.order_tif)
                order = Order(f'bt-{intent.client_order_id}', intent)
                orders.append(order)
                pending.append(order)
                ages[order.order_id] = 0
        if any(order.status in ('PENDING', 'PARTIALLY_FILLED') for order in orders):
            warnings.append('Unfilled GTC remainders remain open at the end of the simulation; no assumed liquidation.')
        return BacktestResult(curve, orders, ledger.fills, ledger.snapshot(), warnings=warnings, synthetic=dataset.synthetic)
    finally:
        risk.close()
