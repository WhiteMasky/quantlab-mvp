import type { PlatformState } from './types';

export class ApiError extends Error {
  constructor(message: string, public status?: number) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? { Accept: 'application/json' } : {
        Accept: 'application/json', 'Content-Type': 'application/json',
      },
      credentials: 'same-origin',
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal,
    });
  } catch (error) {
    if (error && typeof error === 'object' && 'name' in error && error.name === 'AbortError') throw error;
    throw new ApiError('无法连接服务。请刷新页面检查登录状态及服务进程；未生成任何替代结果，请勿重复提交上一操作。');
  }
  if (response.status === 401) {
    throw new ApiError('访问会话已过期或未授权。请刷新页面完成邮箱验证；请勿重复提交上一操作。', 401);
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError(`服务返回了非 JSON 响应（HTTP ${response.status}）。`, response.status);
  }
  if (!response.ok) {
    const detail = payload && typeof payload === 'object' && 'detail' in payload
      ? (payload as { detail: unknown }).detail : payload;
    throw new ApiError(`HTTP ${response.status} · ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`, response.status);
  }
  return payload as T;
}

export async function fetchState(signal?: AbortSignal): Promise<PlatformState> {
  const data = await request<PlatformState>('/api/state', undefined, signal);
  if (!data || typeof data !== 'object' || !data.data || !data.portfolio || !data.risk || !data.broker
    || !Array.isArray(data.orders) || !Array.isArray(data.fills) || !Array.isArray(data.experiments)
    || !Array.isArray(data.audit) || !Array.isArray(data.alerts) || !Array.isArray(data.portfolio.positions)) {
    throw new ApiError('状态响应不符合平台契约；操作已禁用，请检查后端。');
  }
  return data;
}
