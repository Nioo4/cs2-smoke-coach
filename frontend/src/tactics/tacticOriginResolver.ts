import { accountRequest, type Notebook } from './api';
import type { Team } from './teamApi';

interface Origin { label: string; to: string }
export async function resolveTacticOrigin(notebookId: string, signal: AbortSignal): Promise<Origin> {
  const personal = await accountRequest<{ notebook: Notebook }>('notebooks/personal', 'GET', undefined, undefined, signal);
  if (!personal.notebook?.id || personal.notebook.kind !== 'personal') throw Error('invalid_catalog');
  if (personal.notebook.id === notebookId) return { label: '我的个人战术本', to: '/tactics' };
  let cursor: string | null = null;
  const seen = new Set<string>();
  do {
    const params = new URLSearchParams({ limit: '50' }); if (cursor) params.set('cursor', cursor);
    const page: { items: Team[]; next_cursor: string | null } = await accountRequest(`teams?${params}`, 'GET', undefined, undefined, signal);
    if (!Array.isArray(page.items) || (page.next_cursor !== null && typeof page.next_cursor !== 'string')) throw Error('invalid_catalog');
    const team = page.items.find(item => item.notebook_id === notebookId);
    if (team) {
      if (!team.id || typeof team.name !== 'string' || !team.name.trim()) throw Error('invalid_team');
      return { label: `队伍：${team.name}`, to: `/teams/${encodeURIComponent(team.id)}` };
    }
    cursor = page.next_cursor;
    if (cursor) { if (seen.has(cursor)) throw Error('repeated_cursor'); seen.add(cursor); }
  } while (cursor && !signal.aborted);
  throw Error('origin_unknown');
}
