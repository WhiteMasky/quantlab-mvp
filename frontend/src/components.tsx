import { useId, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowDownRight, ArrowUpRight, ChevronDown, Clock3, Database, FlaskConical, Info, ShieldCheck, Terminal } from 'lucide-react';
import type { Benchmark, ChartPoint, Details, Numeric, PlatformState, RunResult } from './types';

export function numeric(value: unknown): number | null {
  if (value === undefined || value === null || (typeof value === 'string' && !value.trim()) || typeof value === 'boolean') return null;
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function number(value: unknown, digits = 2): string {
  const parsed = numeric(value);
  return parsed === null ? '—' : new Intl.NumberFormat('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(parsed);
}

export function money(value: unknown): string {
  return numeric(value) === null ? '—' : `$${number(value)}`;
}

export function percent(value: unknown, digits = 2): string {
  const parsed = numeric(value);
  return parsed === null ? '—' : `${number(parsed * 100, digits)}%`;
}

export function text(value: unknown): string {
  if (value === undefined || value === null || value === '') return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export function dateTime(value: unknown): string {
  if (typeof value !== 'string' || !value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${new Intl.DateTimeFormat('en-GB', { year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'UTC', hour12: false }).format(date)} UTC`;
}

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'blue' | 'green' | 'amber' | 'red' }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Empty({ title = '暂无数据', children, icon = 'chart' }: { title?: string; children?: ReactNode; icon?: 'chart' | 'data' | 'clock' }) {
  const Icon = icon === 'data' ? Database : icon === 'clock' ? Clock3 : FlaskConical;
  return <div className="empty-state"><div className="empty-icon"><Icon size={24} strokeWidth={1.4} /></div><strong>{title}</strong><p>{children ?? '运行离线实验后，实际结果将在这里呈现。'}</p></div>;
}

export function Panel({ title, eyebrow, action, children, className = '' }: { title: string; eyebrow?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}><div className="panel-heading"><div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h2>{title}</h2></div>{action}</div>{children}</section>;
}

export function Stat({ label, value, subtitle, accent, icon }: { label: string; value: string; subtitle: string; accent?: boolean; icon?: ReactNode }) {
  return <div className={`stat-card ${accent ? 'stat-accent' : ''}`}><div className="stat-label">{label}{icon}</div><div className="stat-value">{value}</div><div className="stat-subtitle">{subtitle}</div></div>;
}

export function Disclosure({ title, data, open = false }: { title: string; data: unknown; open?: boolean }) {
  return <details className="disclosure" open={open || undefined}><summary>{title}<ChevronDown size={15} /></summary><pre>{JSON.stringify(data, null, 2) ?? '暂无返回数据'}</pre></details>;
}

export function Facts({ items }: { items: [string, unknown][] }) {
  return <dl className="facts">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{text(value)}</dd></div>)}</dl>;
}

export function Notices({ messages, title = '数据与方法限制' }: { messages: string[]; title?: string }) {
  if (!messages.length) return null;
  return <div className="notice"><Info size={17} /><div><strong>{title}</strong><ul>{messages.map((message, i) => <li key={`${i}-${message}`}>{message}</li>)}</ul></div></div>;
}

export function Validation({ run, compact = false }: { run: RunResult | null | undefined; compact?: boolean }) {
  const verdict = text(run?.validation?.verdict);
  return <div className={`validation-card ${run ? 'validation-returned' : ''}`}><div className="validation-heading"><ShieldCheck size={19} /><span>验证结论 / VALIDATION</span></div>
    {run ? <><Badge tone="amber">{run.validation?.passed === false ? 'VALIDATION FAILED' : 'RESEARCH ONLY'}</Badge><h3>{verdict}</h3><p>{run.synthetic ? '合成数据不构成投资证据。即使收益为正，也不代表策略存在统计优势。' : '请结合样本、成本与样本外证据审阅，禁止据此启用实盘。'}</p>{!compact && <Disclosure title="完整验证结果与限制" data={run.validation} />}</>
      : <><h3>尚未验证</h3><p>等待真实计算结果。这里不会预设收益、评分或通过状态。</p></>}
    <div className="validation-foot"><span className="status-dot amber" />No live execution · 不构成投资建议</div>
  </div>;
}

export function EquityChart({ points, benchmarks = {}, initialCash }: { points: ChartPoint[]; benchmarks?: Record<string, Benchmark>; initialCash?: Numeric }) {
  const [mode, setMode] = useState<'equity' | 'drawdown'>('equity');
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [hidden, setHidden] = useState<string[]>([]);
  const id = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const colors = ['#5a9bff', '#4cd7b4', '#a5a4db', '#e4b76a'];
  const clean = (curve: ChartPoint[]) => curve.filter(point => numeric(point.equity) !== null && Number.isFinite(Date.parse(point.at))).sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const transform = (curve: ChartPoint[], startingCash?: Numeric) => {
    let peak = numeric(startingCash) ?? numeric(curve[0]?.equity) ?? 0;
    return curve.map(point => {
      const equity = Number(point.equity);
      peak = Math.max(peak, equity);
      return { at: point.at, time: Date.parse(point.at), value: mode === 'equity' ? equity : peak > 0 ? (equity / peak - 1) * 100 : 0 };
    });
  };
  const data = transform(clean(points), initialCash);
  const comparison = Object.entries(benchmarks).filter(([, benchmark]) => Array.isArray(benchmark.equity_curve)).map(([key, benchmark], index) => ({
    key, label: key === 'buy_and_hold' ? '等权买入持有' : key === 'cash' ? '现金基准' : key,
    description: benchmark.label, color: colors[1 + index % (colors.length - 1)], data: transform(clean(benchmark.equity_curve!)),
  })).filter(series => series.data.length > 0);
  const series = [{ key: 'strategy', label: '策略', description: '历史回测，非当前账户', color: colors[0], data }, ...comparison];
  const visible = series.filter(line => !hidden.includes(line.key));
  const all = visible.flatMap(line => line.data);
  const values = all.map(point => point.value);
  const rawMin = values.length ? Math.min(...values) : 0, rawMax = values.length ? Math.max(...values) : 0;
  const padding = Math.max((rawMax - rawMin) * 0.15, mode === 'equity' ? 1 : 0.1);
  const min = rawMin - padding, max = rawMax + padding;
  const times = series.flatMap(line => line.data.map(point => point.time));
  const firstTime = times.length ? Math.min(...times) : 0, lastTime = times.length ? Math.max(...times) : 1;
  const width = 760, height = 292, left = 70, right = 18, top = 18, bottom = 38;
  const x = (time: number) => left + ((time - firstTime) / Math.max(lastTime - firstTime, 1)) * (width - left - right);
  const y = (value: number) => top + (1 - (value - min) / (max - min)) * (height - top - bottom);
  const path = (curve: typeof data) => curve.map((point, i) => `${i === 0 ? 'M' : 'L'}${x(point.time).toFixed(2)},${y(point.value).toFixed(2)}`).join(' ');
  const area = data.length ? `${path(data)} L${x(data[data.length - 1].time)},${height - bottom} L${x(data[0].time)},${height - bottom} Z` : '';
  const index = Math.min(activeIndex ?? data.length - 1, data.length - 1);
  const active = data[index];
  const format = (value: number) => mode === 'equity' ? money(value) : `${number(value)}%`;
  return <div className="chart-block"><div className="chart-toolbar"><div className="chart-legend"><span className="legend-dot" />{mode === 'equity' ? '历史净值 · USD' : '历史回撤 · %'}</div><div className="segmented" aria-label="图表指标"><button type="button" aria-pressed={mode === 'equity'} onClick={() => setMode('equity')}>净值</button><button type="button" aria-pressed={mode === 'drawdown'} onClick={() => setMode('drawdown')}>回撤</button></div></div>
    {!data.length ? <div className="chart-empty"><div className="empty-grid" aria-hidden="true" /><Empty title="让证据先于结论">运行一次离线实验，查看实际计算的净值、基准曲线与回撤。</Empty></div> : <><div className="series-legend">{series.map(line => <button key={line.key} title={line.description} aria-pressed={!hidden.includes(line.key)} disabled={line.key === 'strategy'} onClick={() => setHidden(previous => previous.includes(line.key) ? previous.filter(key => key !== line.key) : [...previous, line.key])}><span style={{ background: line.color }} />{line.label}</button>)}</div><svg className="equity-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${mode === 'equity' ? '历史回测净值' : '历史回撤'}曲线，${data.length} 个实际观测点`} onPointerMove={event => {
      const bounds = event.currentTarget.getBoundingClientRect();
      const pixel = (event.clientX - bounds.left) / bounds.width * width;
      const time = firstTime + (pixel - left) / (width - left - right) * (lastTime - firstTime);
      const nearest = data.reduce((best, point, i) => Math.abs(point.time - time) < Math.abs(data[best].time - time) ? i : best, 0);
      setActiveIndex(nearest);
    }}><title>{mode === 'equity' ? '历史回测净值' : '历史回撤'}，非当前模拟账本。仅绘制后端实际观测。</title><defs><linearGradient id={id} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={colors[0]} stopOpacity="0.19" /><stop offset="100%" stopColor={colors[0]} stopOpacity="0.01" /></linearGradient></defs>
      {[0, 1, 2, 3, 4].map(tick => { const value = min + (max - min) * tick / 4; return <g key={tick}><line className="chart-gridline" x1={left} x2={width - right} y1={y(value)} y2={y(value)} strokeDasharray="3 5" /><text x={left - 12} y={y(value) + 4} textAnchor="end" className="chart-axis">{mode === 'equity' ? number(value, 0) : `${number(value, 1)}%`}</text></g>; })}
      <path d={area} fill={`url(#${id})`} />{visible.map(line => <g key={line.key}><path aria-label={`${line.label}曲线`} d={path(line.data)} fill="none" stroke={line.color} strokeWidth={line.key === 'strategy' ? '2.5' : '1.7'} strokeDasharray={line.key === 'cash' ? '4 5' : undefined} strokeLinejoin="round" strokeLinecap="round" />{line.data.length === 1 && <circle cx={x(line.data[0].time)} cy={y(line.data[0].value)} r="3" fill={line.color} />}</g>)}
      {active && <g><line x1={x(active.time)} x2={x(active.time)} y1={top} y2={height - bottom} className="chart-crosshair" strokeDasharray="3 4" /><circle cx={x(active.time)} cy={y(active.value)} r="4" fill={colors[0]} stroke="#121b2b" strokeWidth="2" /></g>}
      {[...new Set([0, Math.floor((data.length - 1) / 2), data.length - 1])].map(i => <text key={i} x={x(data[i].time)} y={height - 10} textAnchor={i === 0 ? 'start' : i === data.length - 1 ? 'end' : 'middle'} className="chart-axis">{data[i].at.slice(0, 10)}</text>)}
    </svg><div className="chart-inspector"><label htmlFor={`observation-${id}`}>观测点 <span>{active?.at.slice(0, 10)} UTC</span></label><input id={`observation-${id}`} aria-label="查看曲线观测点" type="range" min="0" max={data.length - 1} value={Math.max(0, index)} onChange={event => setActiveIndex(Number(event.target.value))} /><div className="chart-values">{visible.map(line => { const point = line.data.find(point => point.time === active?.time); return <span key={line.key}><i style={{ background: line.color }} />{line.label}<strong>{point ? format(point.value) : '—'}</strong></span>; })}</div></div><div className="chart-caption"><span>{data.length} observations · 非当前账户</span><span>{mode === 'drawdown' ? '按已返回曲线的历史峰值计算' : comparison.length ? '基准均使用后端实际曲线' : '未返回基准曲线，不绘制替代值'}</span></div></>}
  </div>;
}

export function Exposure({ portfolio }: { portfolio: PlatformState['portfolio'] }) {
  const equity = numeric(portfolio.equity), cash = numeric(portfolio.cash);
  const cashWeight = equity !== null && equity > 0 && cash !== null ? cash / equity : null;
  const positions = portfolio.positions.map(position => ({ symbol: position.symbol, weight: equity !== null && equity > 0 && numeric(position.market_price) !== null ? Number(position.market_price) * position.quantity / equity : null }));
  const weights = positions.map(position => position.weight).filter((weight): weight is number => weight !== null);
  const concentration = positions.length === 0 && equity !== null && equity > 0 ? 0 : weights.length > 0 && weights.length === positions.length ? Math.max(...weights) : null;
  return <Panel title="资金与风险敞口" eyebrow="EXPOSURE MONITOR" action={<Badge>当前模拟账本</Badge>}><div className="exposure-stats"><div><span>总敞口 / Equity</span><strong>{percent(portfolio.exposure, 1)}</strong><small>后端账本值</small></div><div><span>现金占比</span><strong>{percent(cashWeight, 1)}</strong><small>现金 ÷ 当前净值</small></div><div><span>最大单一持仓</span><strong>{percent(concentration, 1)}</strong><small>按当前标记价格计算</small></div></div>{equity !== null && equity > 0 ? <div className="exposure-breakdown">{[...positions, { symbol: 'CASH', weight: cashWeight }].map((position, index) => <div className="exposure-row" key={position.symbol}><span>{position.symbol}</span><div className="exposure-track"><i style={{ width: `${Math.max(0, Math.min(100, (position.weight ?? 0) * 100))}%`, background: position.symbol === 'CASH' ? 'var(--mint)' : `hsl(${214 + index * 17} 75% 68%)` }} /></div><strong>{percent(position.weight, 1)}</strong></div>)}</div> : <p className="panel-note">尚无有效净值，无法计算敞口占比。</p>}<div className="panel-bottom"><span>敞口不是风险评分；不推算缺失的限额或压力损失。</span></div></Panel>;
}

export function MetricDirection({ value }: { value: unknown }) {
  const n = numeric(value);
  return n === null ? <Terminal size={16} /> : n >= 0 ? <ArrowUpRight size={16} /> : <ArrowDownRight size={16} />;
}

export function AuditList({ entries }: { entries: Details[] }) {
  if (!entries.length) return <Empty title="暂无审计事件" icon="clock">执行操作后，后端持久化审计记录将在此显示。</Empty>;
  const timestamp = (entry: Details) => String(entry.at ?? entry.timestamp ?? entry.created_at ?? '');
  return <div className="audit-list">{entries.slice().sort((a, b) => timestamp(b).localeCompare(timestamp(a))).slice(0, 100).map((entry, index) => <article className="audit-item" key={`${String(entry.id ?? entry.event_id ?? 'audit')}-${index}`}><span className="audit-node"><Terminal size={13} /></span><div><div className="audit-meta"><strong>{text(entry.action ?? entry.event ?? entry.type ?? entry.kind ?? 'Audit event')}</strong><time>{dateTime(entry.at ?? entry.timestamp ?? entry.created_at)}</time></div><p>{text(entry.reason ?? entry.message ?? entry.detail ?? entry.result ?? entry.payload)}</p><Disclosure title="事件详情" data={entry} /></div></article>)}</div>;
}
