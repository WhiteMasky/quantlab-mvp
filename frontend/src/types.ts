export type Numeric = string | number;
export type Details = Record<string, unknown>;

export interface Position {
  symbol: string;
  quantity: number;
  average_cost: Numeric;
  market_price: Numeric;
}

export interface Order {
  order_id: string;
  intent: {
    intent_id: string;
    client_order_id: string;
    symbol: string;
    side: string;
    quantity: number;
    reference_price: Numeric;
    created_at: string;
    tif: string;
    mode: string;
  };
  status: string;
  filled_quantity: number;
  reason: string;
}

export interface Fill {
  fill_id: string;
  order_id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: Numeric;
  commission: Numeric;
  at: string;
}

export interface EquityPoint {
  at: string;
  cash: Numeric;
  equity: Numeric;
  gross_exposure: Numeric;
}

export type ChartPoint = Pick<EquityPoint, 'at' | 'equity'>;

export interface Benchmark {
  label?: string;
  equity_curve?: ChartPoint[];
  metrics?: Details;
  total_return?: Numeric;
  annualized_return?: Numeric;
}

export interface RunResult {
  data_version?: string;
  code_version?: string;
  seed?: number;
  run_id: string;
  strategy: string;
  parameters: Details;
  synthetic: boolean;
  result: {
    equity_curve?: EquityPoint[];
    curve?: EquityPoint[];
    metrics?: Details;
    benchmarks?: Record<string, Benchmark>;
    warnings?: string[];
    orders?: Order[];
    fills?: Fill[];
  };
  validation: Details;
  execution: Details;
  warnings: string[];
}

export interface PlatformState {
  environment: string;
  live_enabled: boolean;
  broker: Details;
  data: {
    source?: string;
    synthetic?: boolean;
    version?: string;
    symbols?: string[];
    start?: string;
    end?: string;
    issues?: (Details | string)[];
    limitations?: string[];
    updated_at?: string;
  };
  portfolio: {
    cash?: Numeric;
    equity?: Numeric;
    positions: Position[];
    exposure?: Numeric;
  };
  orders: Order[];
  fills: Fill[];
  experiments: Details[];
  risk: Details;
  audit: Details[];
  latest_run: RunResult | null;
  alerts: string[];
}

export type Page = 'overview' | 'data' | 'research' | 'portfolio' | 'orders' | 'risk';
export type Strategy = 'trend' | 'momentum';
