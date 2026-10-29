import type {
  AdminLineupOut,
  LineupCreatePayload,
  LineupDetail,
  MapResponse,
  PointCreatePayload,
  PointOut,
  LineupPatchPayload,
  MetricsSummary,
  PointPatchPayload,
  TargetPositionPayload,
  SearchRequest,
  SearchResponse,
  TargetChoices,
  TranscriptionResponse,
} from '../types/api';

const API_ROOT = '/api/v1';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  readonly requestId: string | null;

  constructor(message: string, status: number, code: string, details: unknown, requestId: string | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

type RequestOptions = Omit<RequestInit, 'body'> & { body?: BodyInit | null };

async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set('Accept', 'application/json');
  const hasBody = options.body !== undefined && options.body !== null;
  if (hasBody && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${API_ROOT}${path}`, { ...options, headers });
  const requestId = response.headers.get('X-Request-ID');
  let payload: unknown;
  try {
    payload = await response.json();
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('服务器返回了无法读取的响应。', response.status, 'INVALID_RESPONSE', null, requestId);
  }
  if (!response.ok) {
    const error = payload && typeof payload === 'object' && 'error' in payload
      ? (payload as { error?: { message?: string; code?: string; details?: unknown; request_id?: string } }).error
      : undefined;
    throw new ApiError(
      error?.message ?? '请求暂时失败，请稍后重试。',
      response.status,
      error?.code ?? 'REQUEST_FAILED',
      error?.details ?? null,
      error?.request_id ?? requestId,
    );
  }
  if (payload === null || typeof payload !== 'object') {
    throw new ApiError('服务器返回了无法读取的响应。', response.status, 'INVALID_RESPONSE', null, requestId);
  }
  return payload as T;
}

export function countCodePoints(value: string): number {
  return [...value].length;
}

export function encodeBasicUtf8(username: string, password: string): string {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return `Basic ${btoa(binary)}`;
}

export const publicApi = {
  map: (signal?: AbortSignal) => requestJson<MapResponse>('/maps/mirage', { signal }),
  targetLineups: (slug: string, signal?: AbortSignal) =>
    requestJson<TargetChoices>(`/targets/${encodeURIComponent(slug)}/lineups`, { signal }),
  detail: (id: string, signal?: AbortSignal) => requestJson<LineupDetail>(`/lineups/${id}`, { signal }),
  search: (body: SearchRequest, signal?: AbortSignal) =>
    requestJson<SearchResponse>('/search', { method: 'POST', body: JSON.stringify(body), signal }),
  transcription: (audio: Blob, signal?: AbortSignal) => {
    const body = new FormData();
    body.append('audio', audio, 'recording.webm');
    return requestJson<TranscriptionResponse>('/transcriptions', { method: 'POST', body, signal });
  },
};

function adminHeaders(authorization: string): HeadersInit {
  return { Authorization: authorization, Accept: 'application/json' };
}

export const adminApi = {
  session: (authorization: string, signal?: AbortSignal) =>
    requestJson<{ authenticated: true }>('/admin/session', {
      headers: adminHeaders(authorization),
      signal,
    }),
  map: (authorization: string, signal?: AbortSignal) =>
    requestJson<MapResponse['map']>('/admin/maps/mirage', {
      headers: adminHeaders(authorization),
      signal,
    }),
  targets: (authorization: string, signal?: AbortSignal, includeArchived = false) =>
    requestJson<PointOut[]>(includeArchived ? '/admin/targets?include_archived=true' : '/admin/targets', { headers: adminHeaders(authorization), signal }),
  origins: (authorization: string, signal?: AbortSignal, includeArchived = false) =>
    requestJson<PointOut[]>(includeArchived ? '/admin/origins?include_archived=true' : '/admin/origins', { headers: adminHeaders(authorization), signal }),
  lineups: (authorization: string, signal?: AbortSignal) =>
    requestJson<AdminLineupOut[]>('/admin/lineups?status=all', { headers: adminHeaders(authorization), signal }),
  createTarget: (authorization: string, body: PointCreatePayload) =>
    requestJson<PointOut>('/admin/targets', {
      method: 'POST', headers: adminHeaders(authorization), body: JSON.stringify(body),
    }),
  createOrigin: (authorization: string, body: PointCreatePayload) =>
    requestJson<PointOut>('/admin/origins', {
      method: 'POST', headers: adminHeaders(authorization), body: JSON.stringify(body),
    }),
  createLineup: (authorization: string, body: LineupCreatePayload) =>
    requestJson<AdminLineupOut>('/admin/lineups', {
      method: 'POST', headers: adminHeaders(authorization), body: JSON.stringify(body),
    }),
  patchLineup: (authorization: string, id: string, body: LineupPatchPayload) =>
    requestJson<AdminLineupOut>(`/admin/lineups/${id}`, {
      method: 'PATCH', headers: adminHeaders(authorization), body: JSON.stringify(body),
    }),
  uploadVideo: (authorization: string, id: string, file: File, rightsNote: string) => {
    const body = new FormData();
    body.append('file', file, file.name);
    body.append('rights_note', rightsNote);
    return requestJson<AdminLineupOut>(`/admin/lineups/${id}/video`, {
      method: 'POST', headers: adminHeaders(authorization), body,
    });
  },
  patchTarget: (authorization: string, id: string, body: PointPatchPayload) =>
    requestJson<PointOut>(`/admin/targets/${id}`, {
      method: 'PATCH', headers: adminHeaders(authorization), body: JSON.stringify(body),
    }),
  patchOrigin: (authorization: string, id: string, body: PointPatchPayload) =>
    requestJson<PointOut>(`/admin/origins/${id}`, {
      method: 'PATCH', headers: adminHeaders(authorization), body: JSON.stringify(body),
    }),
  positionTarget: (authorization: string, id: string, body: TargetPositionPayload) =>
    requestJson<PointOut>(`/admin/targets/${id}/position`, {
      method: 'PATCH', headers: adminHeaders(authorization), body: JSON.stringify(body),
    }),
  archiveTarget: (authorization: string, id: string) =>
    requestJson<PointOut>(`/admin/targets/${id}/archive`, { method: 'POST', headers: adminHeaders(authorization) }),
  restoreTarget: (authorization: string, id: string) =>
    requestJson<PointOut>(`/admin/targets/${id}/restore`, { method: 'POST', headers: adminHeaders(authorization) }),
  archiveOrigin: (authorization: string, id: string) =>
    requestJson<PointOut>(`/admin/origins/${id}/archive`, { method: 'POST', headers: adminHeaders(authorization) }),
  restoreOrigin: (authorization: string, id: string) =>
    requestJson<PointOut>(`/admin/origins/${id}/restore`, { method: 'POST', headers: adminHeaders(authorization) }),
  metrics: (authorization: string, signal?: AbortSignal) =>
    requestJson<MetricsSummary>('/admin/metrics/summary', { headers: adminHeaders(authorization), signal }),
  setDefault: (authorization: string, id: string) =>
    requestJson<AdminLineupOut>(`/admin/lineups/${id}/set-default`, { method: 'POST', headers: adminHeaders(authorization) }),
  unpublish: (authorization: string, id: string) =>
    requestJson<AdminLineupOut>(`/admin/lineups/${id}/unpublish`, { method: 'POST', headers: adminHeaders(authorization) }),
  markNeeds: (authorization: string, id: string) =>
    requestJson<AdminLineupOut>(`/admin/lineups/${id}/mark-needs-revalidation`, { method: 'POST', headers: adminHeaders(authorization) }),
  archiveLineup: (authorization: string, id: string) =>
    requestJson<AdminLineupOut>(`/admin/lineups/${id}/archive`, { method: 'POST', headers: adminHeaders(authorization) }),
  restoreLineup: (authorization: string, id: string) =>
    requestJson<AdminLineupOut>(`/admin/lineups/${id}/restore`, { method: 'POST', headers: adminHeaders(authorization) }),
  archiveVideo: (authorization: string, id: string) =>
    requestJson<AdminLineupOut>(`/admin/lineups/${id}/video/archive`, { method: 'POST', headers: adminHeaders(authorization) }),
  restoreVideo: (authorization: string, id: string) =>
    requestJson<AdminLineupOut>(`/admin/lineups/${id}/video/restore`, { method: 'POST', headers: adminHeaders(authorization) }),
  publish: (authorization: string, id: string, makeDefault: boolean) =>
    requestJson<AdminLineupOut>(`/admin/lineups/${id}/publish`, {
      method: 'POST', headers: adminHeaders(authorization), body: JSON.stringify({ make_default: makeDefault }),
    }),
};
