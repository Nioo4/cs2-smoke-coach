import { ApiError } from '../api/client';

export interface Account { id: string; username: string; display_name: string }
export interface Session { user: Account; csrf_token: string }
export interface Notebook { id: string; kind: 'personal' | 'team'; user_id: string | null; team_id: string | null }

// Each page owns its session token in memory; ordinary account calls never use admin auth.
export async function accountRequest<T>(path: string, method = 'GET', body?: object, csrf?: string, signal?: AbortSignal, idempotencyKey?: string): Promise<T> {
  const response = await fetch(`/api/v1/${path}`, {
    method, credentials: 'same-origin', cache: 'no-store', signal,
    headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}), ...(csrf ? { 'X-CSRF-Token': csrf } : {}), ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const payload = await response.json();
  if (!response.ok) throw new ApiError(payload.error?.message ?? '请求失败，请稍后再试。', response.status, payload.error?.code ?? 'REQUEST_FAILED', null, response.headers.get('X-Request-ID'));
  return payload as T;
}
