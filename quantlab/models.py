from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from enum import Enum
from typing import Any

UTC = timezone.utc
ZERO = Decimal('0')


def money(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite monetary value')
    return result.quantize(Decimal('0.0001'), rounding=ROUND_HALF_EVEN)


def require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError('Timestamp must be timezone-aware UTC')


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    session: date
    open_at: datetime
    close_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_close: Decimal
    volume: int

    def __post_init__(self):
        for timestamp in (self.open_at, self.close_at, self.available_at):
            require_utc(timestamp)


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: date
    known_at: datetime
    split_ratio: Decimal = Decimal('1')
    cash_dividend: Decimal = ZERO


@dataclass
class Dataset:
    version: str
    source: str
    synthetic: bool
    bars: list[MarketBar]
    actions: list[CorporateAction] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Signal:
    symbol: str
    as_of: datetime
    score: float
    eligible: bool
    reason: str


@dataclass(frozen=True)
class TargetWeight:
    symbol: str
    weight: Decimal
    reason: str


@dataclass
class Position:
    symbol: str
    quantity: int
    average_cost: Decimal
    market_price: Decimal


@dataclass
class AccountSnapshot:
    cash: Decimal
    equity: Decimal
    positions: dict[str, Position]
    as_of: datetime
    reconciled: bool = True
    day_start_equity: Decimal = Decimal('100000')
    peak_equity: Decimal = Decimal('100000')


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    created_at: datetime
    reference_price: Decimal
    tif: str = 'DAY'
    mode: str = 'SIMULATED'

    def __post_init__(self):
        require_utc(self.created_at)
        if self.side not in ('BUY', 'SELL') or self.quantity <= 0 or type(self.quantity) is not int:
            raise ValueError('Positive whole-share BUY/SELL orders only')
        if not self.reference_price.is_finite() or self.reference_price <= 0:
            raise ValueError('Price must be positive and finite')
        if self.mode not in ('SIMULATED', 'IBKR_PAPER'):
            raise ValueError('Live orders are disabled in this release')
        if self.tif not in ('DAY', 'GTC'):
            raise ValueError('Unsupported time in force')


@dataclass
class Order:
    order_id: str
    intent: OrderIntent
    status: str = 'PENDING'
    filled_quantity: int = 0
    reason: str = ''
    broker_order_id: int | None = None


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    quantity: int
    price: Decimal
    commission: Decimal
    at: datetime


@dataclass(frozen=True)
class EquityPoint:
    at: datetime
    cash: Decimal
    equity: Decimal
    gross_exposure: Decimal


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: Decimal = Decimal('100000')
    fee_per_share: Decimal = Decimal('0.005')
    minimum_fee: Decimal = Decimal('1')
    spread_bps: Decimal = Decimal('2')
    slippage_bps: Decimal = Decimal('5')
    max_participation: Decimal = Decimal('0.01')
    fee_assumption: str = 'Illustrative software-test assumptions, not a current IBKR tariff; USD 0.005/share, USD 1 minimum, 2 bps full spread, 5 bps adverse slippage.'
    order_tif: str = 'DAY'
    max_order_age_sessions: int = 20
    rebalance_every: int = 1


@dataclass
class BacktestResult:
    equity_curve: list[EquityPoint]
    orders: list[Order]
    fills: list[Fill]
    final_account: AccountSnapshot
    metrics: dict[str, Any] = field(default_factory=dict)
    benchmarks: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    synthetic: bool = True
