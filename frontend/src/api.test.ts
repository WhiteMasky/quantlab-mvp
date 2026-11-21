import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, fetchState, request } from './api';

const fetchMock = vi.fn<typeof fetch>();
beforeEach(() => { vi.stubGlobal('fetch', fetchMock); fetchMock.mockReset(); });
afterEach(() => vi.unstubAllGlobals());

describe('same-origin contract client', () => {
  it('sends local JSON mutations without credentials or a live endpoint', async () => {
    fetchMock.mockResolvedValue(new Response('{"ok":true}', { status: 200 }));
    expect(await request('/api/runs', { strategy: 'trend', lookback: 63, seed: 7 })).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledWith('/api/runs', expect.objectContaining({
      method: 'POST', credentials: 'same-origin',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: '{"strategy":"trend","lookback":63,"seed":7}',
    }));
  });
  it('preserves structured backend errors rather than returning fake results', async () => {
    fetchMock.mockResolvedValue(new Response('{"detail":{"code":"job_busy","message":"Try refresh"}}', { status: 409 }));
    await expect(request('/api/runs', {})).rejects.toThrow(/HTTP 409.*job_busy/);
  });
  it('rejects an HTML proxy response', async () => {
    fetchMock.mockResolvedValue(new Response('<html>proxy unavailable</html>', { status: 502 }));
    await expect(request('/api/state')).rejects.toThrow(/非 JSON.*502/);
  });
  it('rejects incompatible snapshot structures', async () => {
    fetchMock.mockResolvedValue(new Response('{"environment":"SIMULATED","portfolio":{"positions":[]}}', { status: 200 }));
    await expect(fetchState()).rejects.toThrow(/不符合平台契约/);
  });
  it('reports a disconnection and never retries a mutation automatically', async () => {
    fetchMock.mockRejectedValue(new TypeError('NetworkError'));
    await expect(request('/api/risk/halt', { halted: true, reason: 'test' })).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('propagates aborts so StrictMode cleanup does not appear as an API failure', async () => {
    const error = new DOMException('Aborted', 'AbortError');
    fetchMock.mockRejectedValue(error);
    await expect(request('/api/state', undefined, new AbortController().signal)).rejects.toBe(error);
  });
});
