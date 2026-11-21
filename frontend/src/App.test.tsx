import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import type { PlatformState, RunResult } from './types';

// Explicit test fixtures only; production never imports or falls back to these.
function emptyState(): PlatformState {
  return {
    environment: 'SIMULATED', live_enabled: false, broker: { connected: false },
    data: { synthetic: true, source: 'Not loaded', symbols: [], issues: [], limitations: [] },
    portfolio: { positions: [] }, orders: [], fills: [], experiments: [],
    risk: { halted: false }, audit: [], latest_run: null, alerts: [],
  };
}
const run: RunResult = {
  run_id: 'test-run-42', strategy: 'momentum', parameters: { lookback: 126 }, seed: 42,
  synthetic: true, data_version: 'test-data-version', code_version: 'test-code-version',
  result: {
    metrics: { total_return: 0.2, sharpe: null, max_drawdown: -0.03, sample_size: 2 },
    equity_curve: [
      { at: '2024-01-02T21:00:00Z', equity: '100000', cash: '100000', gross_exposure: '0' },
      { at: '2024-01-03T21:00:00Z', equity: '120000', cash: '20000', gross_exposure: '0.8' },
    ],
    benchmarks: { buy_and_hold: { label: 'Test benchmark', equity_curve: [
      { at: '2024-01-02T21:00:00Z', equity: '100000' },
      { at: '2024-01-03T21:00:00Z', equity: '110000' },
    ] } },
  },
  validation: { passed: false, verdict: 'NOT_VALIDATED_SYNTHETIC' },
  execution: { orders: 1, fills: 1 }, warnings: ['Fixture warning from the backend'],
};
function completedState(): PlatformState {
  return {
    ...emptyState(), latest_run: structuredClone(run),
    data: { synthetic: true, source: 'synthetic fixture', version: 'test-data-version', symbols: ['SPY'], issues: [] },
    portfolio: { cash: '101000', equity: '102000', exposure: '0.0098039', positions: [{ symbol: 'SPY', quantity: 10, average_cost: '99', market_price: '100' }] },
    orders: [{ order_id: 'order-test-1', status: 'FILLED', reason: '', filled_quantity: 10, intent: { intent_id: 'intent-test-1', client_order_id: 'client-test-1', symbol: 'SPY', side: 'BUY', quantity: 10, reference_price: '100', created_at: '2024-01-03T14:30:00Z', tif: 'DAY', mode: 'SIMULATED' } }],
    fills: [{ fill_id: 'fill-test-1', order_id: 'order-test-1', symbol: 'SPY', side: 'BUY', quantity: 10, price: '100', commission: '1', at: '2024-01-03T14:30:00Z' }],
    experiments: [{ run_id: run.run_id, status: 'completed', strategy: 'momentum', parameters: { lookback: 126 }, started_at: '2024-01-03T14:00:00Z' }],
  };
}
function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
const fetchMock = vi.fn<typeof fetch>();
const postCalls = () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
async function openApp(page = 'overview', state = emptyState()) {
  window.history.replaceState(null, '', `/#${page}`);
  fetchMock.mockImplementation(async () => json(state));
  render(<App />);
  await waitFor(() => expect(screen.queryByText('读取本地工作区')).not.toBeInTheDocument());
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  window.history.replaceState(null, '', '/');
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('local console and honest state', () => {
  it('shows all six navigation pages, permanent safety labels, and no fabricated metrics', async () => {
    await openApp();
    const navigation = screen.getByRole('complementary', { name: '主导航' });
    expect(within(navigation).getAllByRole('button')).toHaveLength(6);
    expect(screen.getByText('SYNTHETIC DATA')).toBeInTheDocument();
    expect(screen.getByText('IBKR 未连接 · LIVE DISABLED')).toBeInTheDocument();
    expect(screen.getByText('尚未验证')).toBeInTheDocument();
    expect(screen.getByText('让证据先于结论')).toBeInTheDocument();
    expect(screen.queryByText(/\$[0-9]/)).not.toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    const user = userEvent.setup();
    for (const [label, heading] of [['数据管理', '数据管理.'], ['策略研究', '策略研究.'], ['组合持仓', '组合与持仓.'], ['订单与成交', '订单与成交.'], ['风控与审计', '控制边界，而非隐藏风险.']] as const) {
      await user.click(within(navigation).getByRole('button', { name: label }));
      expect(screen.getByRole('heading', { name: new RegExp(heading.slice(0, -1)), level: 1 })).toBeInTheDocument();
      expect(within(navigation).getByRole('button', { name: label })).toHaveAttribute('aria-current', 'page');
    }
  });

  it('does not present an unloaded dataset as a successful quality check', async () => {
    await openApp('data');
    expect(screen.getByText('数据尚未检查')).toBeInTheDocument();
    expect(screen.queryByText(/后端未报告数据缺陷/)).not.toBeInTheDocument();
  });

  it('shows initial loading without a fallback result', async () => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValue(pending.promise);
    render(<App />);
    expect(screen.getByText('读取本地工作区')).toBeInTheDocument();
    expect(screen.queryByText(/\$[0-9]/)).not.toBeInTheDocument();
    await act(async () => pending.resolve(json(emptyState())));
    expect(screen.getByText('尚未验证')).toBeInTheDocument();
  });

  it('recovers from a disconnected API only on explicit refresh', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('offline'));
    render(<App />);
    expect(await screen.findByText('暂时无法读取工作区')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('未生成任何替代结果');
    expect(postCalls()).toHaveLength(0);
    fetchMock.mockResolvedValue(json(emptyState()));
    await userEvent.click(screen.getByRole('button', { name: '重新连接本地服务' }));
    expect(await screen.findByText('尚未验证')).toBeInTheDocument();
  });

  it('closes the mobile drawer after navigation', async () => {
    await openApp();
    await userEvent.click(screen.getByRole('button', { name: '打开导航' }));
    expect(screen.getByRole('complementary')).toHaveClass('sidebar-open');
    await userEvent.click(screen.getByRole('button', { name: '数据管理' }));
    expect(screen.getByRole('complementary')).not.toHaveClass('sidebar-open');
  });
});

describe('bounded, explicit offline experiment', () => {
  it.each([
    ['回看窗口', '9'], ['回看窗口', '127'], ['回看窗口', '63.5'],
    ['随机种子', '-1'], ['随机种子', '2147483648'], ['随机种子', '0.5'],
  ])('rejects invalid %s %s before any POST', async (label, value) => {
    await openApp('research');
    fireEvent.change(screen.getByRole('spinbutton', { name: new RegExp(label) }), { target: { value } });
    expect(screen.getByRole('button', { name: '运行离线实验' })).toBeDisabled();
    expect(postCalls()).toHaveLength(0);
  });

  it.each([['10', '0'], ['126', '2147483647']])('accepts engine boundary lookback=%s seed=%s', async (lookback, seed) => {
    await openApp('research');
    fireEvent.change(screen.getByRole('spinbutton', { name: /回看窗口/ }), { target: { value: lookback } });
    fireEvent.change(screen.getByRole('spinbutton', { name: /随机种子/ }), { target: { value: seed } });
    expect(screen.getByRole('button', { name: '运行离线实验' })).toBeEnabled();
  });

  it('sends edited integer parameters once, disables duplicates, and refreshes actual results', async () => {
    await openApp('research');
    const pending = deferred<Response>();
    fetchMock.mockImplementation(async (url, init) => init?.method === 'POST' ? pending.promise : json(completedState()));
    const user = userEvent.setup();
    await user.click(screen.getByRole('radio', { name: /横截面动量/ }));
    fireEvent.change(screen.getByRole('spinbutton', { name: /回看窗口/ }), { target: { value: '126' } });
    fireEvent.change(screen.getByRole('spinbutton', { name: /随机种子/ }), { target: { value: '42' } });
    await user.click(screen.getByRole('button', { name: '运行离线实验' }));
    expect(postCalls()).toHaveLength(1);
    expect(postCalls()[0][0]).toBe('/api/runs');
    expect(JSON.parse(postCalls()[0][1]!.body as string)).toEqual({ strategy: 'momentum', lookback: 126, seed: 42 });
    const pendingButton = screen.getByRole('button', { name: '计算中，请勿重复提交…' });
    expect(pendingButton).toBeDisabled();
    expect(screen.getByRole('spinbutton', { name: /回看窗口/ })).toBeDisabled();
    await user.click(pendingButton);
    expect(postCalls()).toHaveLength(1);
    expect(screen.getByText('尚未验证')).toBeInTheDocument();
    await act(async () => pending.resolve(json(run)));
    expect(await screen.findByText('NOT_VALIDATED_SYNTHETIC')).toBeInTheDocument();
    expect(screen.getByText('VALIDATION FAILED')).toBeInTheDocument();
    expect(screen.getByText('20.00%')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /2 个实际观测点/ })).toBeInTheDocument();
    expect(screen.getByLabelText('等权买入持有曲线')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '总览' }));
    expect(screen.getByText('$102,000.00')).toBeInTheDocument();
    // Current ledger must never be replaced by the backtest's $120,000 endpoint.
    expect(screen.getByText('模拟账户净值').parentElement?.parentElement).toHaveTextContent('$102,000.00');
  });

  it.each([{ environment: 'UNKNOWN', live_enabled: false }, { environment: 'SIMULATED', live_enabled: true }])('fails closed for an unsafe environment %j', async flags => {
    await openApp('research', { ...emptyState(), ...flags });
    expect(screen.getByText('环境安全检查未通过')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '运行离线实验' })).toBeDisabled();
    expect(postCalls()).toHaveLength(0);
  });

  it('keeps prior evidence but blocks retry after uncertain mutation failure', async () => {
    await openApp('research', completedState());
    fetchMock.mockResolvedValueOnce(json({ detail: 'Execution failed; inspect audit' }, 500));
    await userEvent.click(screen.getByRole('button', { name: '运行离线实验' }));
    expect(await screen.findByText(/HTTP 500/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '运行离线实验' })).toBeDisabled();
    expect(screen.getByText('NOT_VALIDATED_SYNTHETIC')).toBeInTheDocument();
    expect(postCalls()).toHaveLength(1);
    fetchMock.mockResolvedValue(json(completedState()));
    await userEvent.click(screen.getByRole('button', { name: '刷新核实' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '运行离线实验' })).toBeEnabled());
    expect(postCalls()).toHaveLength(1);
  });
});

describe('separate halt and cancellation safety boundaries', () => {
  it('keeps emergency halt available while a research POST is still running', async () => {
    await openApp('research');
    const pending = deferred<Response>();
    fetchMock.mockImplementation(async (url, init) => {
      if (url === '/api/runs') return pending.promise;
      if (init?.method === 'POST') return json({ halted: true });
      return json({ ...completedState(), risk: { halted: true } });
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '运行离线实验' }));
    await user.click(screen.getByRole('button', { name: '风控与审计' }));
    expect(screen.getByRole('button', { name: '设置 Halt' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '授权撤单' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '设置 Halt' }));
    await user.type(screen.getByLabelText('操作原因 / Audit reason'), 'Stop during research');
    await user.click(screen.getByRole('button', { name: '确认设置 Halt' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('HALTED · 新订单暂停。')).toBeInTheDocument();
    expect(postCalls()).toHaveLength(2);
    expect(JSON.parse(postCalls()[1][1]!.body as string).halted).toBe(true);
    expect(screen.getByRole('button', { name: '申请解除 Halt' })).toBeDisabled();
    await act(async () => pending.resolve(json(run)));
    await waitFor(() => expect(screen.getByRole('button', { name: '申请解除 Halt' })).toBeEnabled());
  });

  it('requires a halt reason, persists the returned state, and never sends cancellation', async () => {
    await openApp('risk', completedState());
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '设置 Halt' }));
    const dialog = screen.getByRole('dialog', { name: '确认暂停新订单' });
    expect(dialog).toHaveTextContent('Halt 只阻止新的订单');
    expect(within(dialog).getByRole('button', { name: '确认设置 Halt' })).toBeDisabled();
    const reason = within(dialog).getByLabelText('操作原因 / Audit reason');
    fireEvent.change(reason, { target: { value: 'Two\nlines' } });
    expect(within(dialog).getByRole('button', { name: '确认设置 Halt' })).toBeDisabled();
    await user.clear(reason);
    await user.type(reason, 'Operator pause');
    fetchMock.mockImplementation(async (url, init) => init?.method === 'POST' ? json({ halted: true }) : json({ ...completedState(), risk: { halted: true, reason: 'Operator pause' } }));
    await user.click(within(dialog).getByRole('button', { name: '确认设置 Halt' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(postCalls()).toHaveLength(1);
    expect(postCalls()[0][0]).toBe('/api/risk/halt');
    expect(JSON.parse(postCalls()[0][1]!.body as string)).toEqual({ halted: true, reason: 'Operator pause' });
    expect(screen.getByRole('button', { name: '申请解除 Halt' })).toBeInTheDocument();
    expect(screen.getByText('HALTED · 新订单暂停。')).toBeInTheDocument();
  });

  it('requires separate explicit consent for cancellation and permits safe dismissal', async () => {
    await openApp('risk');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '授权撤单' }));
    let dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('已有持仓不会被平仓');
    expect(within(dialog).getByRole('button', { name: '确认授权撤单' })).toBeDisabled();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(postCalls()).toHaveLength(0);
    expect(screen.getByRole('button', { name: '授权撤单' })).toHaveFocus();
    await user.click(screen.getByRole('button', { name: '授权撤单' }));
    dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('checkbox'));
    await user.click(within(dialog).getByRole('button', { name: '确认授权撤单' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(postCalls()).toHaveLength(1);
    expect(postCalls()[0][0]).toBe('/api/orders/cancel');
    expect(JSON.parse(postCalls()[0][1]!.body as string)).toEqual({ authorized: true });
  });
});

describe('execution logs', () => {
  it('filters actual orders and fills, preserving honest no-match states', async () => {
    await openApp('orders', completedState());
    expect(screen.getByText('order-test-1')).toBeInTheDocument();
    const search = screen.getByRole('textbox', { name: '搜索标的或记录 ID' });
    await userEvent.type(search, 'missing');
    expect(screen.getByText('没有匹配的订单')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '导出当前记录' })).toBeDisabled();
    await userEvent.clear(search);
    screen.getByRole('tab', { name: /订单/ }).focus();
    await userEvent.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: /成交/ })).toHaveFocus();
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'fills-tab');
    expect(screen.getByText('fill-test-1')).toBeInTheDocument();
    expect(screen.getByText('$1.00')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: '订单状态筛选' })).not.toBeInTheDocument();
  });
});
