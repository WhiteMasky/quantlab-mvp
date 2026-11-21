import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { AuditList, EquityChart, Exposure, money, number, numeric, percent } from './components';

// Small fixtures exercise rendering, not investment performance.
const points = [
  { at: '2024-01-02T21:00:00Z', equity: '100' },
  { at: '2024-01-03T21:00:00Z', equity: '120' },
  { at: '2024-01-04T21:00:00Z', equity: '90' },
];
afterEach(cleanup);

describe('numeric evidence', () => {
  it.each([null, undefined, '', ' ', 'NaN', Infinity, true, {}, []])('never turns missing/invalid %j into a metric', value => {
    expect(numeric(value)).toBeNull();
    expect(money(value)).toBe('—');
    expect(number(value)).toBe('—');
    expect(percent(value)).toBe('—');
  });
  it('accepts decimal API strings and actual zeros', () => {
    expect(money('100000.0000')).toBe('$100,000.00');
    expect(percent('0')).toBe('0.00%');
    expect(percent('-0.125')).toBe('-12.50%');
  });
});

describe('equity and benchmark evidence', () => {
  it('renders an honest empty chart without a synthetic placeholder SVG', () => {
    render(<EquityChart points={[]} />);
    expect(screen.getByText('让证据先于结论')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
  it('plots only valid observations, including actual benchmarks, and supports keyboard inspection', async () => {
    render(<EquityChart points={[...points, { at: 'invalid', equity: '999' }, { at: '2024-01-05T21:00:00Z', equity: 'NaN' }]} benchmarks={{ buy_and_hold: { equity_curve: points.map(point => ({ ...point, equity: '100' })) }, no_curve: { total_return: '0.99' } }} />);
    expect(screen.getByRole('img', { name: /3 个实际观测点/ })).toBeInTheDocument();
    expect(screen.getByLabelText('等权买入持有曲线')).toBeInTheDocument();
    expect(screen.queryByLabelText('no_curve曲线')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '等权买入持有' }));
    expect(screen.queryByLabelText('等权买入持有曲线')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '回撤' }));
    expect(screen.getByText('-25.00%')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('slider', { name: '查看曲线观测点' }), { target: { value: '0' } });
    expect(screen.getByText('0.00%')).toBeInTheDocument();
    expect(screen.getByText('2024-01-02 UTC')).toBeInTheDocument();
  });
  it('handles a single constant observation without non-finite SVG coordinates', () => {
    render(<EquityChart points={[points[0]]} />);
    expect(screen.getByRole('img')).not.toContainHTML('NaN');
    expect(screen.getByLabelText('策略曲线').getAttribute('d')).not.toMatch(/NaN|Infinity/);
    expect(screen.getByText('未返回基准曲线，不绘制替代值')).toBeInTheDocument();
  });
});

describe('exposure and audit', () => {
  it('does not infer exposure from missing account values', () => {
    render(<Exposure portfolio={{ positions: [] }} />);
    expect(screen.getByText('尚无有效净值，无法计算敞口占比。')).toBeInTheDocument();
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument();
  });
  it('calculates weights only from returned cash, marks, and equity', () => {
    render(<Exposure portfolio={{ cash: '600', equity: '1000', exposure: '0.4', positions: [{ symbol: 'SPY', quantity: 4, market_price: '100', average_cost: '95' }] }} />);
    expect(screen.getAllByText('40.0%')).toHaveLength(3);
    expect(screen.getAllByText('60.0%')).toHaveLength(2);
    expect(screen.getByText('SPY')).toBeInTheDocument();
  });
  it('sorts mixed audit sources newest first and preserves engine payloads', () => {
    render(<AuditList entries={[
      { id: 1, at: '2024-01-02T00:00:00Z', event: 'EARLIER', payload: 'first payload' },
      { id: 1, at: '2024-01-04T00:00:00Z', event: 'LATEST', payload: 'halt only, no liquidation' },
    ]} />);
    expect(screen.getAllByRole('article')[0]).toHaveTextContent('LATEST');
    expect(screen.getByText('halt only, no liquidation')).toBeInTheDocument();
  });
});
