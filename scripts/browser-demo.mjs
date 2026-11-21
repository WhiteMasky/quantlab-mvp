import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs';

const baseURL = 'http://127.0.0.1:15173';
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1 });
const page = await context.newPage();
const errors = [];
const evidence = { started_at: new Date().toISOString(), baseURL, browser: browser.version(), checks: [], research: {} };
page.on('pageerror', error => errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
const check = name => { evidence.checks.push(name); console.log(`PASS ${name}`); };
const apiState = async () => (await context.request.get(`${baseURL}/api/state`)).json();
const navigation = page.getByRole('complementary', { name: '主导航' });
const go = async name => navigation.getByRole('button', { name, exact: true }).click();
async function screenshot(name, fullPage = false) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: `reports/${name}.png`, fullPage, animations: 'disabled' });
}
async function run(strategy) {
  await go('策略研究');
  await page.getByRole('radio', { name: strategy === 'trend' ? /趋势跟随/ : /横截面动量/ }).check();
  const response = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST', { timeout: 120000 });
  await page.getByRole('button', { name: '运行离线实验', exact: true }).click();
  assert.equal(await page.getByRole('button', { name: '计算中，请勿重复提交…' }).isDisabled(), true);
  const result = await response;
  assert.equal(result.status(), 200, await result.text());
  const report = await result.json();
  await page.getByRole('button', { name: '运行离线实验', exact: true }).waitFor({ state: 'visible' });
  assert.equal(report.validation.passed, false);
  assert.equal(report.validation.verdict, 'NOT_VALIDATED_SYNTHETIC');
  return report;
}

try {
  await page.goto(baseURL);
  await page.getByRole('heading', { name: /让每一次决策/ }).waitFor();
  await page.getByText('$100,000.00', { exact: true }).waitFor();
  const initial = await apiState();
  assert.equal(initial.latest_run, null, 'Use a new empty runtime for this acceptance script; never delete existing evidence to rerun it.');
  await screenshot('demo-empty', true);
  check('empty state has real initial cash and no fabricated return');
  await go('策略研究');
  await page.getByRole('spinbutton', { name: /回看窗口/ }).fill('9');
  assert.equal(await page.getByRole('button', { name: '运行离线实验' }).isDisabled(), true);
  await page.getByRole('spinbutton', { name: /回看窗口/ }).fill('63');
  check('invalid lookback rejected before submission');
  const trend = await run('trend');
  evidence.research.trend = { run_id: trend.run_id, metrics: trend.result.metrics, verdict: trend.validation.verdict,
    selected_parameters: trend.validation.selected_parameters, execution: trend.execution };
  await writeFile('reports/demo-trend.json', JSON.stringify(trend));
  assert.ok(trend.execution.fills > 0);
  const afterTrend = await apiState();
  await go('总览');
  await page.getByRole('img', { name: /504 个实际观测点/ }).waitFor();
  await screenshot('demo-overview', true);
  await page.getByRole('button', { name: '回撤', exact: true }).click();
  await screenshot('demo-drawdown');
  await page.getByRole('button', { name: '净值', exact: true }).click();
  check('trend real computation, equity, benchmark, drawdown and simulated fills');
  for (const [name, file] of [['数据管理', 'demo-data'], ['策略研究', 'demo-research'], ['组合持仓', 'demo-portfolio'], ['订单与成交', 'demo-orders'], ['风控与审计', 'demo-risk']]) {
    await go(name); await screenshot(file);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, name);
  }
  check('all six desktop pages render without horizontal document overflow');
  await go('订单与成交');
  await page.getByRole('textbox', { name: '搜索标的或记录 ID' }).fill('NO-MATCH-TEST');
  await page.getByText('没有匹配的订单', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '导出当前记录' }).isDisabled(), true);
  await page.getByRole('textbox', { name: '搜索标的或记录 ID' }).fill('');
  await page.getByRole('tab', { name: /成交/ }).click();
  const downloadEvent = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出当前记录' }).click();
  const download = await downloadEvent;
  await download.saveAs('reports/demo-fills-export.json');
  check('order search, fill tab and actual record download');
  const repeated = await run('trend');
  assert.equal(repeated.replayed_from_sealed_result, true);
  assert.deepEqual((await apiState()).fills, afterTrend.fills);
  check('identical experiment replays sealed evidence without duplicate fills');

  await page.getByRole('radio', { name: /横截面动量/ }).check();
  const momentumResponse = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST', { timeout: 120000 });
  await page.getByRole('button', { name: '运行离线实验', exact: true }).click();
  await go('风控与审计');
  assert.equal(await page.getByRole('button', { name: '设置 Halt' }).isEnabled(), true);
  await page.getByRole('button', { name: '设置 Halt' }).click();
  await page.getByLabel('操作原因 / Audit reason').fill('Browser acceptance: stop during active research');
  const stopResponse = page.waitForResponse(r => r.url().endsWith('/api/risk/halt') && r.request().method() === 'POST');
  const stopStart = Date.now();
  await page.getByRole('button', { name: '确认设置 Halt' }).click();
  const stopped = await stopResponse;
  assert.equal(stopped.status(), 200);
  assert.equal((await stopped.json()).halted, true);
  evidence.halt_confirmation_ms = Date.now() - stopStart;
  const health = await (await context.request.get(`${baseURL}/api/health`)).json();
  assert.equal(health.service.busy, true, 'Stop must be acknowledged before research ends');
  await page.getByText('HALTED · 新订单暂停。', { exact: true }).waitFor();
  check('emergency halt is acknowledged while research is still busy');
  const momentumHTTP = await momentumResponse;
  assert.equal(momentumHTTP.status(), 200, await momentumHTTP.text());
  const momentum = await momentumHTTP.json();
  assert.equal(momentum.execution.orders, 0);
  assert.equal(momentum.execution.fills, 0);
  assert.equal(momentum.validation.passed, false);
  await writeFile('reports/demo-momentum.json', JSON.stringify(momentum));
  evidence.research.momentum = { run_id: momentum.run_id, metrics: momentum.result.metrics,
    verdict: momentum.validation.verdict, selected_parameters: momentum.validation.selected_parameters,
    execution: momentum.execution };
  await page.getByRole('button', { name: '申请解除 Halt' }).waitFor();
  const beforeCancel = await apiState();
  assert.deepEqual(beforeCancel.fills, afterTrend.fills);
  await page.getByRole('button', { name: '授权撤单', exact: true }).click();
  assert.equal(await page.getByRole('button', { name: '确认授权撤单' }).isDisabled(), true);
  await page.getByRole('checkbox').check();
  const cancelResponse = page.waitForResponse(r => r.url().endsWith('/api/orders/cancel'));
  await page.getByRole('button', { name: '确认授权撤单' }).click();
  assert.equal((await cancelResponse).status(), 200);
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  assert.deepEqual((await apiState()).portfolio, beforeCancel.portfolio);
  check('halt prevents new submissions; cancellation separately authorized and never liquidates');
  await screenshot('demo-halted');
  await page.getByRole('button', { name: '申请解除 Halt' }).click();
  await page.getByLabel('操作原因 / Audit reason').fill('Browser acceptance complete: resume simulated-only admission');
  const resumeResponse = page.waitForResponse(r => r.url().endsWith('/api/risk/halt'));
  await page.getByRole('button', { name: '确认解除 Halt' }).click();
  assert.equal((await resumeResponse).status(), 200);
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  await go('策略研究');
  await page.getByRole('radio', { name: /趋势跟随/ }).check();
  await run('trend');
  await go('总览');

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => getComputedStyle(document.querySelector('.sidebar')).visibility === 'hidden');
  await screenshot('demo-mobile', true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.getByRole('button', { name: '打开导航' }).click();
  await go('订单与成交');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  check('390px mobile menu, overview and order table keep content within viewport');
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.route('**/api/state', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: 'Injected local UI availability test' } }) }));
  await page.getByRole('button', { name: '刷新状态', exact: true }).click();
  await page.getByText('请求失败 / 状态待核实', { exact: true }).waitFor();
  await screenshot('demo-error');
  await page.unroute('**/api/state');
  await page.getByRole('button', { name: '刷新核实', exact: true }).click();
  await page.getByText('请求失败 / 状态待核实', { exact: true }).waitFor({ state: 'hidden' });
  check('injected 503 preserves stale evidence and explicit refresh recovers');
  const unexpectedErrors = errors.filter(message => !message.includes('503 (Service Unavailable)'));
  assert.deepEqual(unexpectedErrors, []);
  evidence.console_errors = errors;
  evidence.finished_at = new Date().toISOString();
  evidence.status = 'passed';
  await writeFile('reports/browser-acceptance.json', JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify(evidence));
} catch (error) {
  evidence.status = 'failed'; evidence.error = String(error); evidence.console_errors = errors;
  await writeFile('reports/browser-acceptance.json', JSON.stringify(evidence, null, 2));
  throw error;
} finally {
  await browser.close();
}
