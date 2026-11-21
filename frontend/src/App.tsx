import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { Activity, ArrowRight, BookOpen, Check, CircleAlert, Command, Database, FlaskConical, LayoutDashboard, LockKeyhole, Menu, Pause, RefreshCw, ShieldCheck, Wallet, X, XCircle } from 'lucide-react';
import { fetchState, request } from './api';
import { Badge } from './components';
import { DataPage, OrdersPage, Overview, PortfolioPage, ResearchPage, RiskPage } from './pages';
import type { Page, PlatformState, RunResult, Strategy } from './types';
import './styles.css';

const navigation = [
  { id: 'overview' as const, label: '总览', english: 'Overview', icon: LayoutDashboard },
  { id: 'data' as const, label: '数据管理', english: 'Data', icon: Database },
  { id: 'research' as const, label: '策略研究', english: 'Research', icon: FlaskConical },
  { id: 'portfolio' as const, label: '组合持仓', english: 'Portfolio', icon: Wallet },
  { id: 'orders' as const, label: '订单与成交', english: 'Execution', icon: Activity },
  { id: 'risk' as const, label: '风控与审计', english: 'Risk & audit', icon: ShieldCheck },
];

function pageFromHash(): Page {
  const hash = window.location.hash.replace(/^#\/?/, '');
  return navigation.find(item => item.id === hash)?.id ?? 'overview';
}

type DialogKind = 'halt' | 'resume' | 'cancel';

export default function App() {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [mobileNav, setMobileNav] = useState(false);
  const [state, setState] = useState<PlatformState | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<'run' | 'halt' | 'cancel' | null>(null);
  const [strategy, setStrategy] = useState<Strategy>('trend');
  const [lookback, setLookback] = useState('63');
  const [seed, setSeed] = useState('7');
  const [dialog, setDialog] = useState<DialogKind | null>(null);
  const mutationLock = useRef(false);
  const haltLock = useRef(false);
  const [haltBusy, setHaltBusy] = useState(false);
  const initialRequest = useRef<AbortController | null>(null);
  const headingRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const change = () => { setPage(pageFromHash()); setMobileNav(false); };
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal): Promise<boolean> => {
    try {
      const next = await fetchState(signal);
      if (signal?.aborted) return false;
      setState(next); setStale(false); setError(null);
      return true;
    } catch (cause) {
      if (signal?.aborted) return false;
      setStale(true); setError(cause instanceof Error ? cause.message : '状态读取失败。');
      return false;
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController(); initialRequest.current = controller;
    void refresh(controller.signal).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refresh]);

  const navigate = (next: Page) => {
    setPage(next); window.location.hash = next; setMobileNav(false);
    headingRef.current?.focus();
  };
  const safeEnvironment = state?.environment === 'SIMULATED' && state?.live_enabled === false;
  const canMutate = Boolean(state && safeEnvironment && !stale && !loading && !refreshing && !busy && !haltBusy);
  const canEmergencyHalt = Boolean(state && safeEnvironment && !loading && !haltBusy && (busy === null || busy === 'run'));

  async function emergencyStop(reason: string) {
    if (haltLock.current || !canEmergencyHalt) return;
    haltLock.current = true; setHaltBusy(true); setError(null);
    try {
      const risk = await request<Record<string, unknown>>('/api/risk/halt', { halted: true, reason });
      setState(previous => previous ? { ...previous, risk } : previous);
      setDialog(null);
      setNotice('紧急停止已由后端确认；新订单暂停，已有订单与持仓不变。');
      if (!mutationLock.current) await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '停止状态未确认，请检查后端风控。');
      setStale(true); setDialog(null);
    } finally {
      haltLock.current = false; setHaltBusy(false);
    }
  }

  async function mutate(kind: 'run' | 'halt' | 'cancel', path: string, body: unknown) {
    if (mutationLock.current || !canMutate) return;
    mutationLock.current = true; setBusy(kind); setError(null); setNotice(null);
    try {
      const result = await request<RunResult | Record<string, unknown>>(path, body);
      const synchronized = await refresh();
      if (synchronized) {
        setNotice(kind === 'run' ? `离线计算已返回 · ${String(result.run_id ?? '请查看实验登记簿')}。验证结论请单独审阅。` : kind === 'halt' ? 'Halt 设置已返回；当前持仓不受该操作影响。' : '授权撤单请求已返回，请检查每笔订单状态；未发出平仓请求。');
        setDialog(null);
      } else {
        setNotice('操作已返回，但状态同步失败。以下可能为旧快照，请刷新核实；不会自动重试操作。');
        setDialog(null);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '操作失败，未生成替代结果。');
      // The request may have reached the server. Require a fresh snapshot before another mutation.
      setStale(true); setDialog(null);
    } finally {
      mutationLock.current = false; setBusy(null);
    }
  }

  async function manualRefresh() {
    if (busy || refreshing) return;
    initialRequest.current?.abort(); setRefreshing(true); setNotice(null);
    await refresh(); setRefreshing(false); setLoading(false);
  }

  const current = navigation.find(item => item.id === page)!;
  const pageProps = state ? { state, navigate } : null;
  let content: ReactNode;
  if (loading) content = <div className="initial-state" role="status"><span className="spinner large" /><h1>读取本地工作区</h1><p>正在请求 /api/state，不预填任何研究结果。</p><div className="loading-skeleton"><i /><i /><i /><i /></div></div>;
  else if (!state) content = <div className="initial-state"><div className="empty-error"><Database size={30} /></div><div className="eyebrow">LOCAL SERVICE UNAVAILABLE</div><h1>暂时无法读取工作区</h1><p>启动本地服务后重试。没有数据时，不展示收益与持仓占位数字。</p><button className="button primary" onClick={manualRefresh} disabled={refreshing}><RefreshCw size={16} className={refreshing ? 'spin' : ''} />重新连接本地服务</button></div>;
  else if (pageProps) {
    switch (page) {
      case 'data': content = <DataPage {...pageProps} />; break;
      case 'research': content = <ResearchPage {...pageProps} strategy={strategy} setStrategy={setStrategy} lookback={lookback} setLookback={setLookback} seed={seed} setSeed={setSeed} running={busy === 'run'} canMutate={canMutate} onRun={() => void mutate('run', '/api/runs', { strategy, lookback: Number(lookback), seed: Number(seed) })} />; break;
      case 'portfolio': content = <PortfolioPage {...pageProps} />; break;
      case 'orders': content = <OrdersPage {...pageProps} />; break;
      case 'risk': content = <RiskPage {...pageProps} canMutate={canMutate} canHalt={state.risk.halted === true ? canMutate : canEmergencyHalt} busy={Boolean(busy) || haltBusy} onHalt={() => setDialog(state.risk.halted === true ? 'resume' : 'halt')} onCancel={() => setDialog('cancel')} />; break;
      default: content = <Overview {...pageProps} />;
    }
  }

  return <div className="app-shell"><a className="skip-link" href="#main-content" onClick={event => { event.preventDefault(); headingRef.current?.focus(); }}>跳到主内容</a>
    {mobileNav && <button className="nav-backdrop" aria-label="关闭导航" onClick={() => setMobileNav(false)} />}
    <aside id="workspace-navigation" className={`sidebar ${mobileNav ? 'sidebar-open' : ''}`} aria-label="主导航" onKeyDown={event => { if (event.key === 'Escape') setMobileNav(false); }}><a className="brand" href="#overview" onClick={() => navigate('overview')}><div className="brand-mark"><Command size={24} strokeWidth={2} /></div><div><strong>QuantLab<span> /</span></strong><small>RESEARCH & EXECUTION</small></div></a>
      <div className="workspace-picker"><div className="workspace-avatar">Q</div><div><strong>本地研究工作区</strong><span>Local workspace</span></div><LockKeyhole size={13} /></div>
      <div className="nav-label">WORKSPACE</div><nav>{navigation.map(item => <button key={item.id} className={`nav-item ${page === item.id ? 'active' : ''}`} aria-current={page === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}><item.icon size={19} strokeWidth={1.7} /><span>{item.label}</span>{page === item.id && <span className="nav-active-dot" />}</button>)}</nav>
      <div className="sidebar-bottom"><div className="local-card"><div><span className="status-dot blue" /><strong>安全的研究边界</strong></div><p>Research locally.<br />Execute deliberately.</p><span><LockKeyhole size={12} />实盘交易永久禁用</span></div><div className="sidebar-footer"><span className="avatar">QL</span><div><strong>离线研究环境</strong><small>SIMULATED ONLY</small></div><span className="version">v0.1</span></div></div>
    </aside><div className="main-shell"><header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-menu" aria-label="打开导航" aria-controls="workspace-navigation" aria-expanded={mobileNav} onClick={() => setMobileNav(true)}><Menu size={20} /></button><span className="breadcrumb-workspace">Workspace</span><span className="breadcrumb-slash">/</span><strong>{current.english}</strong></div><div className="topbar-right"><span className="utc-label">UTC</span><span className="connection-label"><span className={`status-dot ${state && !stale ? 'green' : 'amber'}`} />{loading ? '连接中' : stale || !state ? '状态未同步' : '本地服务'}</span><button className="icon-button" title="刷新状态" aria-label="刷新状态" disabled={Boolean(busy) || refreshing || loading} onClick={manualRefresh}><RefreshCw size={16} className={refreshing ? 'spin' : ''} /></button><span className="topbar-divider" /><Badge tone="blue">SIMULATED</Badge></div></header>
      <div className="environment-strip"><div><LockKeyhole size={13} /><strong>SIMULATED</strong><span className="strip-divider">/</span><strong>{state?.data.synthetic === false ? 'LOCAL DATA · UNVERIFIED' : 'SYNTHETIC DATA'}</strong><span>仅用于研究与软件验证，不构成投资证据</span></div><span>IBKR 未连接 · LIVE DISABLED</span></div>
      <main className="main-content" id="main-content" tabIndex={-1} ref={headingRef}>
        {error && <div className="message error-message" role="alert"><CircleAlert size={20} /><div><strong>请求失败 / 状态待核实</strong><p>{error}</p>{state && <p>保留上次快照，仅供参考；请刷新确认服务器状态后再操作。</p>}</div><button className="button small secondary" onClick={manualRefresh} disabled={Boolean(busy) || refreshing}>刷新核实</button></div>}
        {state && !safeEnvironment && <div className="message error-message" role="alert"><LockKeyhole size={20} /><div><strong>环境安全检查未通过</strong><p>后端必须明确返回 SIMULATED 且 live_enabled=false；所有写操作已禁用。</p></div></div>}
        {notice && <div className="message info-message" role="status"><Check size={18} /><p>{notice}</p><button className="icon-button" aria-label="关闭通知" onClick={() => setNotice(null)}><X size={16} /></button></div>}
        {busy && <div className="message info-message" role="status"><span className="spinner" /><p>{busy === 'run' ? '正在运行离线实验，仍展示上次已完成的结果。请勿重复提交。' : '操作处理中，等待后端确认。'}</p></div>}
        {state?.risk.halted === true && <div className="message warning-message" role="alert"><Pause size={18} /><p><strong>HALTED · 新订单暂停。</strong>已有订单和持仓不受影响，撤单需另行授权。</p><button className="text-button" onClick={() => navigate('risk')}>查看风控<ArrowRight size={14} /></button></div>}
        {!!state?.alerts?.length && <details className="system-alerts"><summary><CircleAlert size={15} /><strong>运行警示</strong><span>{state.alerts[0]}</span><Badge tone="amber">{state.alerts.length}</Badge></summary><ul>{state.alerts.map((alert, index) => <li key={`${alert}-${index}`}>{alert}</li>)}</ul></details>}
        {content}
        <footer className="main-footer"><span><BookOpen size={13} />QuantLab · Evidence before execution.</span><span>本地优先 · 无外部券商连接 · USD / UTC</span></footer>
      </main></div>
    {dialog && <SafetyDialog kind={dialog} busy={dialog === 'halt' ? haltBusy : Boolean(busy) || haltBusy} onClose={() => { if (!haltBusy && (dialog === 'halt' || !busy)) setDialog(null); }} onConfirm={reason => dialog === 'halt' ? void emergencyStop(reason) : dialog === 'cancel' ? void mutate('cancel', '/api/orders/cancel', { authorized: true }) : void mutate('halt', '/api/risk/halt', { halted: false, reason })} />}
  </div>;
}

function SafetyDialog({ kind, busy, onClose, onConfirm }: { kind: DialogKind; busy: boolean; onClose: () => void; onConfirm: (reason: string) => void }) {
  const [authorized, setAuthorized] = useState(false);
  const [reason, setReason] = useState('');
  const panel = useRef<HTMLDivElement>(null);
  const cancel = kind === 'cancel';
  const resume = kind === 'resume';
  const validReason = reason.trim().length > 0 && !/[\u0000-\u001f\u007f]/.test(reason);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panel.current?.focus();
    return () => { document.body.style.overflow = previousOverflow; previous?.focus(); };
  }, []);
  function submit(event: FormEvent) { event.preventDefault(); if (!busy && (cancel ? authorized : validReason)) onConfirm(reason.trim()); }
  return <div className="dialog-backdrop"><div className="safety-dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title" aria-describedby="dialog-description" tabIndex={-1} ref={panel} onKeyDown={event => {
    if (event.key === 'Escape' && !busy) onClose();
    if (event.key === 'Tab') {
      const focusable = panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled)');
      if (!focusable?.length) { event.preventDefault(); return; }
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) { event.preventDefault(); first.focus(); }
    }
  }}><div className="dialog-top"><div className="dialog-icon">{cancel ? <XCircle size={25} /> : <Pause size={25} />}</div><button className="icon-button" onClick={onClose} aria-label="关闭确认窗口" disabled={busy}><X size={20} /></button></div><div className="eyebrow">SIMULATED · EXPLICIT CONSENT</div><h2 id="dialog-title">{cancel ? '明确授权撤销未完成订单' : resume ? '确认解除新单 Halt' : '确认暂停新订单'}</h2><p id="dialog-description">{cancel ? '此操作只请求撤销尚未完成的模拟订单。已成交部分及已有持仓不会被平仓；每笔撤单结果以服务返回为准。' : resume ? '解除后，策略的新订单仍须通过所有独立风控检查。这不是实盘开关，也不会自动平仓或撤单。' : 'Halt 只阻止新的订单。已有订单继续存在，已有持仓保持不变。若要撤单，必须另行明确授权。'}</p><form onSubmit={submit}>{cancel ? <label className="consent-label"><input type="checkbox" checked={authorized} onChange={event => setAuthorized(event.target.checked)} disabled={busy} /><span>我明确授权撤销未完成的模拟订单，并理解此操作不清仓。</span></label> : <><label className="field-label" htmlFor="halt-reason">操作原因 / Audit reason</label><textarea id="halt-reason" placeholder="填写单行原因，记录到审计日志" value={reason} onChange={event => setReason(event.target.value)} maxLength={500} aria-describedby="reason-help" aria-invalid={reason.length > 0 && !validReason} required disabled={busy} /><p id="reason-help" className={`field-help ${reason.length > 0 && !validReason ? 'error-text' : ''}`}>1–500 字符；不接受空白、换行或控制字符。</p></>}
      <div className="dialog-actions"><button className="button secondary" type="button" onClick={onClose} disabled={busy}>返回，不执行</button><button className="button danger" type="submit" disabled={busy || (cancel ? !authorized : !validReason)}>{busy ? <span className="spinner" /> : <ArrowRight size={16} />}{cancel ? '确认授权撤单' : resume ? '确认解除 Halt' : '确认设置 Halt'}</button></div></form></div></div>;
}
