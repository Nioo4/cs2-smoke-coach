import { ApiError } from '../api/client';
import { accountRequest } from './api';
export interface ShareSummary { id: string; notebook_id: string; tactic_id: string | null; version_id: string; allow_copy: boolean; created_at: string; updated_at: string; revoked_at: string | null }
export interface IssuedShare extends ShareSummary { token: string | null; token_available: boolean }
export interface Manifest { id: string; version_id: string; allow_copy: boolean; published_at: string; items: { item_id: string; title: string; side: 'T' | 'CT'; map: string; source_revision: number }[] }
export interface HistoryVersion { id: string; source_epoch: string; source_revision: number; reason: 'archive' | 'publish' | 'periodic'; created_at: string; expires_at: string }
export const publicShareRequest = <T,>(path: string, body: object, signal?: AbortSignal) => accountRequest<T>(`shares/${path}`, 'POST', body, undefined, signal);
export function shareError(error: unknown) {
  if (!(error instanceof ApiError)) return '暂时无法读取分享，请稍后重试。';
  return ({ SHARE_UNAVAILABLE: '分享已不可用或访问凭据已到期，请重新打开原链接或刷新访问凭据。', SHARE_VERSION_CHANGED: '作者已更新发布版本，请手动刷新后继续；已打开的演示仍固定原版本。', SHARE_SOURCE_CHANGED: '已保存内容已变化，请重新读取并确认发布清单。', SHARE_EMPTY_NOTEBOOK: '战术本为空，不能发布分享。', SHARE_COPY_FORBIDDEN: '当前发布版本不允许复制。', USER_AUTH_REQUIRED: '请先登录后再确认复制。' })[error.code] ?? error.message;
}
