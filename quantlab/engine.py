from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from quantlab.backtest import run_backtest
from quantlab.broker import SimulatedBroker
from quantlab.data import cache_dataset, generate_synthetic, read_dataset, validate_dataset
from quantlab.models import AccountSnapshot, BacktestConfig, Dataset, Position, TargetWeight, jsonable, money
from quantlab.portfolio import allocate, rebalance_orders
from quantlab.research import ExperimentRegistry, candidate_specs
from quantlab.risk import RiskEngine
from quantlab.strategy import generate_signals
from quantlab.validation import performance_metrics, validate_strategy

INITIAL_CASH = Decimal('100000')


def code_version() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    files = sorted((root / 'quantlab').rglob('*.py'))
    files += [root / 'pyproject.toml', root / 'requirements.lock']
    for path in files:
        if path.exists():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:20]


class Platform:
    def __init__(self, runtime_dir: str | Path):
        self.root = Path(runtime_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'platform.sqlite'
        self.lock = threading.RLock()
        self._submission_lock = threading.Lock()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS platform_meta (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS execution_batches (batch_key TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS sealed_holdouts (dataset TEXT NOT NULL, strategy TEXT NOT NULL, parameters TEXT NOT NULL, code_version TEXT NOT NULL, response TEXT, PRIMARY KEY(dataset,strategy))')
            db.execute('CREATE TABLE IF NOT EXISTS platform_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL)')
        self.risk = RiskEngine(self.root / 'risk.sqlite')
        self.broker = SimulatedBroker(self.root / 'broker.sqlite')
        self.registry = ExperimentRegistry(self.root / 'research.sqlite')
        self.dataset = read_dataset(self.root / 'market.parquet') if (self.root / 'market.parquet').exists() else None

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _meta(self, key, default=None):
        with self._db() as db:
            row = db.execute('SELECT payload FROM platform_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row['payload']) if row else default

    def _setmeta(self, key, value):
        with self._db() as db:
            db.execute('INSERT INTO platform_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload', (key, json.dumps(jsonable(value), allow_nan=False)))

    def _audit(self, event, payload):
        with self._db() as db:
            db.execute('INSERT INTO platform_audit(at,event,payload) VALUES (?,?,?)', (datetime.now(timezone.utc).isoformat(), event, json.dumps(jsonable(payload), allow_nan=False)))

    def _account(self, prices=None, as_of=None):
        from quantlab.backtest import Ledger
        prices = prices or {k: Decimal(v) for k, v in self._meta('execution_prices', {}).items()}
        ledger = Ledger(INITIAL_CASH)
        fills = self.broker.fills()
        for fill in sorted(fills, key=lambda item: item.at):
            ledger.apply_fill(fill)
        clock = as_of or (max(fill.at for fill in fills) if fills else datetime.now(timezone.utc))
        account = ledger.mark_to_market(prices, clock)
        account.reconciled = bool(self.broker.status().get('reconciled'))
        account.peak_equity = max(account.peak_equity, Decimal(self._meta('peak_equity', str(INITIAL_CASH))))
        return account

    def snapshot(self):
        with self.lock:
            account = self._account()
            broker_status = jsonable(self.broker.status())
            risk_status = jsonable(self.risk.status())
            dataset = self.dataset
            data = {'source': 'Not loaded', 'synthetic': True, 'version': None, 'symbols': [], 'start': None, 'end': None, 'issues': [], 'limitations': [], 'updated_at': None}
            if dataset:
                sessions = sorted({bar.session for bar in dataset.bars})
                data = {'source': dataset.source, 'synthetic': dataset.synthetic, 'version': dataset.version,
                        'symbols': sorted({bar.symbol for bar in dataset.bars}), 'start': str(sessions[0]), 'end': str(sessions[-1]),
                        'issues': validate_dataset(dataset), 'limitations': dataset.limitations,
                        'updated_at': self._meta('data_updated_at'), 'session_count': len(sessions), 'bar_count': len(dataset.bars)}
            with self._db() as db:
                audit = [dict(row) for row in db.execute('SELECT * FROM platform_audit ORDER BY id DESC LIMIT 200')]
                incomplete = db.execute("SELECT COUNT(*) FROM execution_batches WHERE status='pending'").fetchone()[0]
            alerts = ['仅离线模拟执行 / SIMULATED；IBKR 未连接，券商联调未验证。', '合成行情仅验证软件，不构成策略有效或盈利证据。', '本版本禁止实盘订单；券商连接与策略有效性是两回事。']
            if incomplete:
                alerts.append('存在未完成的执行批次：已阻止重发，需人工核对模拟订单与账本。')
            return jsonable({'environment': 'SIMULATED', 'live_enabled': False, 'broker': broker_status, 'data': data,
                'portfolio': {'cash': account.cash, 'equity': account.equity, 'positions': list(account.positions.values()),
                    'exposure': (account.equity - account.cash) / account.equity if account.equity else 0,
                    'unrealized_pnl': sum(((p.market_price-p.average_cost)*p.quantity for p in account.positions.values()), Decimal('0')),
                    'label': '独立模拟执行账本，不是回测历史末期持仓'},
                'orders': self.broker.orders(), 'fills': self.broker.fills(), 'experiments': self.registry.list_experiments(),
                'risk': risk_status, 'audit': audit + list(jsonable(self.risk.audit())), 'latest_run': self._meta('latest_run'), 'alerts': alerts})

    @staticmethod
    def _targets(strategy, parameters):
        def targets(bars, as_of, account):
            signals = generate_signals(bars, as_of, strategy=strategy, lookback=parameters['lookback'], skip=parameters.get('skip', 5), top_n=parameters.get('top_n', 2))
            current = {symbol: p.quantity * p.market_price / account.equity for symbol, p in account.positions.items()} if account.equity > 0 else {}
            return allocate(signals, bars, as_of, current_weights=current)
        return targets

    def _backtest(self, dataset, strategy, parameters, cost_multiplier=1.0):
        config = BacktestConfig()
        multiplier = Decimal(str(cost_multiplier))
        config = replace(config, fee_per_share=config.fee_per_share*multiplier, minimum_fee=config.minimum_fee*multiplier,
                         spread_bps=config.spread_bps*multiplier, slippage_bps=config.slippage_bps*multiplier)
        result = run_backtest(dataset, self._targets(strategy, parameters), config)
        result.metrics = performance_metrics(result.equity_curve, result.fills, config.initial_cash)
        return result

    @staticmethod
    def _benchmarks(dataset):
        symbols = sorted({bar.symbol for bar in dataset.bars})
        initiated = False
        def hold(bars, as_of, account):
            nonlocal initiated
            if initiated:
                return None
            initiated = True
            return [TargetWeight(symbol, Decimal('0.90')/len(symbols), 'Equal weight buy-and-hold, 10% initial cash') for symbol in symbols]
        config = BacktestConfig()
        result = run_backtest(dataset, hold, config)
        return {'buy_and_hold': {'label': 'Equal-weight universe, initially 90% invested, raw prices and identical costs',
                    'metrics': performance_metrics(result.equity_curve, result.fills, INITIAL_CASH), 'equity_curve': jsonable(result.equity_curve)},
                'cash': {'label': 'USD cash, assumed zero interest', 'total_return': 0, 'annualized_return': 0,
                    'equity_curve': [{'at': p.at.isoformat(), 'equity': str(INITIAL_CASH)} for p in result.equity_curve]}}

    def _execute(self, dataset, strategy, parameters):
        previous_dataset = self._meta('execution_dataset')
        if previous_dataset and previous_dataset != dataset.version:
            return {'orders': 0, 'fills': 0, 'blocked': 'Execution ledger is bound to a different data version; research only, use a separate runtime directory for another simulation account.'}
        sessions = sorted({bar.session for bar in dataset.bars})
        previous = [bar for bar in dataset.bars if bar.session == sessions[-2]]
        current = {bar.symbol: bar for bar in dataset.bars if bar.session == sessions[-1]}
        as_of = max(bar.available_at for bar in previous)
        clock = min(bar.open_at for bar in current.values())
        prices = {symbol: bar.open for symbol, bar in current.items()}
        key = hashlib.sha256(json.dumps([dataset.version, strategy, parameters], sort_keys=True).encode()).hexdigest()
        with self._db() as db:
            row = db.execute('SELECT status FROM execution_batches WHERE batch_key=?', (key,)).fetchone()
            pending = db.execute("SELECT COUNT(*) FROM execution_batches WHERE status='pending'").fetchone()[0]
        if row:
            return {'orders': 0, 'fills': 0, 'deduplicated': True, 'batch_status': row['status']}
        if pending:
            return {'orders': 0, 'fills': 0, 'blocked': 'Uncertain prior execution batch; reconcile manually before new submissions'}
        with self._db() as db:
            db.execute('INSERT INTO execution_batches VALUES (?,?,?)', (key, 'pending', datetime.now(timezone.utc).isoformat()))
        self._setmeta('execution_dataset', dataset.version)
        self._setmeta('execution_prices', prices)
        account = self._account(prices, clock)
        known = [bar for bar in dataset.bars if bar.available_at <= as_of]
        targets = self._targets(strategy, parameters)(known, as_of, account)
        intents = rebalance_orders(targets, account, prices, clock, key)
        submitted, filled, rejected = 0, 0, []
        for intent in intents:
            account = self._account(prices, clock)
            pending_orders = sum(order.status not in ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED') for order in self.broker.orders())
            with self._submission_lock:
                decision = self.risk.check(intent, account, prices[intent.symbol], clock, clock, open_orders=pending_orders, market_open=True)
                if not decision.allowed:
                    rejected.append({'symbol': intent.symbol, 'reasons': decision.reasons})
                    self._audit('RISK_REJECTION', {'client_order_id': intent.client_order_id, 'reasons': decision.reasons})
                    continue
                order = self.broker.submit(intent)
            submitted += 1
            if order.status in ('REJECTED', 'UNKNOWN', 'CANCELLED', 'EXPIRED'):
                self._audit('EXECUTION_BLOCKED', {'order_id': order.order_id, 'status': order.status})
                continue
            direction = Decimal('1') if intent.side == 'BUY' else Decimal('-1')
            fill_price = money(prices[intent.symbol] * (1 + direction * Decimal('0.0006')))
            commission = max(Decimal('1'), Decimal('0.005')*intent.quantity)
            if intent.side == 'BUY' and fill_price*intent.quantity+commission > account.cash:
                self._audit('EXECUTION_BLOCKED', {'order_id': order.order_id, 'reason': 'Insufficient cash after costs'})
                continue
            fill = self.broker.fill(order.order_id, intent.quantity, fill_price, commission, clock, fill_id=f'{order.order_id}:fill:1')
            filled += int(fill is not None)
        account = self._account(prices, clock)
        self._setmeta('peak_equity', str(account.peak_equity))
        with self._db() as db:
            db.execute("UPDATE execution_batches SET status='complete' WHERE batch_key=?", (key,))
        self._audit('SIMULATED_EXECUTION', {'batch': key, 'orders': submitted, 'fills': filled, 'clock': clock, 'rejected': rejected})
        return {'orders': submitted, 'fills': filled, 'rejections': rejected, 'clock': clock.isoformat(), 'mode': 'HISTORICAL_SYNTHETIC_REPLAY'}

    def run_demo(self, strategy='trend', lookback=63, seed=7):
        self._validate_parameters(strategy, lookback, seed)
        return self._run_dataset(generate_synthetic(seed=seed, sessions=504), strategy, lookback, seed,
                                 execute_simulated=True)

    @staticmethod
    def _validate_parameters(strategy, lookback, seed):
        if (strategy not in ('trend', 'momentum') or type(lookback) is not int or
                not 10 <= lookback <= 126 or type(seed) is not int or not 0 <= seed <= 2147483647):
            raise ValueError('Invalid strategy, lookback (10..126), or seed')

    def research_file(self, path, strategy='trend', lookback=63, seed=7, *, synthetic=False):
        from quantlab.data import CSVDataSource, ParquetDataSource
        self._validate_parameters(strategy, lookback, seed)
        path = Path(path)
        if path.suffix.lower() == '.csv':
            dataset = CSVDataSource(synthetic=synthetic).load(path)
        elif path.suffix.lower() == '.parquet':
            dataset = ParquetDataSource().load(path)
        else:
            raise ValueError('Only strict CSV or QuantLab Parquet caches are accepted')
        return self._run_dataset(dataset, strategy, lookback, seed, execute_simulated=False)

    def _run_dataset(self, dataset, strategy, lookback, seed, *, execute_simulated):
        with self.lock:
            parameters = {'lookback': lookback, 'skip': 5, 'top_n': 2}
            self.dataset = dataset
            cache_dataset(dataset, self.root / 'market.parquet')
            self._setmeta('data_updated_at', datetime.now(timezone.utc).isoformat())
            version = code_version()
            run_id = self.registry.start(dataset.version, version, strategy, parameters, seed, dataset.synthetic)
            self._audit('RESEARCH_STARTED', {'run_id': run_id, 'strategy': strategy, 'parameters': parameters, 'synthetic': dataset.synthetic})
            try:
                encoded_parameters = json.dumps({'parameters': parameters, 'seed': seed,
                                                 'execute_simulated': execute_simulated}, sort_keys=True)
                with self._db() as db:
                    prior = db.execute('SELECT * FROM sealed_holdouts WHERE dataset=? AND strategy=?', (dataset.version, strategy)).fetchone()
                    if prior is None:
                        db.execute('INSERT INTO sealed_holdouts VALUES (?,?,?,?,NULL)', (dataset.version, strategy, encoded_parameters, version))
                if prior:
                    if prior['parameters'] != encoded_parameters or prior['code_version'] != version or not prior['response']:
                        raise ValueError('Holdout sealed: this dataset/strategy cannot be retuned or retried after test access; use only training/validation research or a genuinely new, untouched dataset.')
                    response = json.loads(prior['response'])
                    response['run_id'] = run_id
                    response['replayed_from_sealed_result'] = True
                    response['execution'] = {'orders': 0, 'fills': 0, 'deduplicated': True}
                    self.registry.finish(run_id, {'metrics': response['result']['metrics'], 'validation': response['validation'], 'replayed_from_sealed_result': True})
                    self._setmeta('latest_run', response)
                    self._audit('SEALED_RESULT_REPLAY', {'run_id': run_id})
                    return response
                result = self._backtest(dataset, strategy, parameters)
                result.benchmarks = self._benchmarks(dataset)
                def evaluate(subset: Dataset, params: dict, cost_multiplier: float):
                    trial_parameters = {**parameters, **params, 'cost_multiplier': cost_multiplier,
                                        'phase': subset.role, 'parent_run_id': run_id}
                    trial_id = self.registry.start(dataset.version, version, strategy, trial_parameters, seed, dataset.synthetic)
                    try:
                        trial = self._backtest(subset, strategy, {**parameters, **params}, cost_multiplier)
                        self.registry.finish(trial_id, {'metrics': trial.metrics,
                            'scored_start': subset.evaluation_start, 'scored_end': subset.evaluation_end,
                            'metric_scope': 'includes warmup; parent validation contains rebased scored-period metrics'})
                        return trial
                    except Exception as error:
                        self.registry.finish(trial_id, {}, status='failed', error=str(error))
                        raise
                specification = next(spec for spec in candidate_specs() if spec['strategy'] == strategy)
                grid = specification['parameter_grid']
                if parameters not in grid:
                    grid = [parameters] + grid
                validation = validate_strategy(dataset, evaluate, grid, seed=seed)
                execution = self._execute(dataset, strategy, parameters) if execute_simulated else {
                    'orders': 0, 'fills': 0, 'mode': 'RESEARCH_ONLY', 'blocked': 'Imported data research never submits orders.'}
                provenance_warning = ('SYNTHETIC DATA: all prices and any resulting returns are fabricated test inputs, NOT historical market returns.'
                                      if dataset.synthetic else 'LOCAL DATA: provenance, survivorship and point-in-time adjustment history are unverified; statistical gates are not investment approval.')
                response = jsonable({'run_id': run_id, 'strategy': strategy, 'parameters': parameters, 'synthetic': dataset.synthetic,
                    'data_version': dataset.version, 'code_version': version, 'seed': seed,
                    'result': result, 'validation': validation, 'execution': execution,
                    'warnings': list(dict.fromkeys(dataset.limitations + result.warnings + [
                        provenance_warning,
                        'No strategy has passed investment validation; historical profitability would not guarantee future profitability.',
                        'The displayed full-period curve uses requested parameters; validation reports its separately selected parameters and held-out results.',
                        BacktestConfig().fee_assumption,
                        'Imported data is research-only; demo execution is separately labelled historical simulation, never an IBKR paper fill.']))})
                with self._db() as db:
                    db.execute('UPDATE sealed_holdouts SET response=? WHERE dataset=? AND strategy=?', (json.dumps(response, allow_nan=False), dataset.version, strategy))
                self.registry.finish(run_id, {'metrics': result.metrics, 'validation': validation, 'execution': execution})
                self._setmeta('latest_run', response)
                self._audit('RESEARCH_COMPLETED', {'run_id': run_id, 'verdict': validation.get('verdict')})
                return response
            except Exception as error:
                self.registry.finish(run_id, {}, status='failed', error=str(error))
                self._audit('RESEARCH_FAILED', {'run_id': run_id, 'error': str(error)})
                raise

    def emergency_halt(self, reason):
        with self._submission_lock:
            self.risk.set_halt(True, reason)
            self._audit('MANUAL_HALT', {'reason': reason, 'does_not_cancel_or_liquidate': True})
            return jsonable(self.risk.status())

    def set_halt(self, halted, reason):
        if type(halted) is not bool:
            raise ValueError('halted must be boolean')
        if halted:
            return self.emergency_halt(reason)
        with self.lock, self._submission_lock:
            self.risk.set_halt(False, reason)
            self._audit('MANUAL_RESUME', {'reason': reason, 'does_not_cancel_or_liquidate': True})
            return jsonable(self.risk.status())

    def close(self):
        self.risk.close()
        self.broker.close()

    def cancel_all(self, authorized=False):
        if not authorized:
            raise PermissionError('Separate explicit cancellation authorization required')
        with self.lock:
            cancelled = []
            for order in self.broker.orders():
                if order.status not in ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED'):
                    cancelled.append(self.broker.cancel(order.order_id, authorized=True))
            self._audit('MANUAL_CANCEL', {'count': len(cancelled), 'no_liquidation': True})
            return {'cancelled': jsonable(cancelled), 'count': len(cancelled), 'no_liquidation': True}
