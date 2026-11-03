import { useEffect, useLayoutEffect, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import AccountPage from './AccountPage';
import SharePage from './SharePage';

export default function TacticsGate({ publicShare = false }: { publicShare?: boolean }) {
  const route = useLocation();
  const navigate = useNavigate();
  function incomingToken() {
    const tokens = new URLSearchParams(route.hash.slice(1)).getAll('token');
    return tokens.length === 1 && /^[A-Za-z0-9_-]{43}$/.test(tokens[0]) ? tokens[0] : null;
  }
  const [entry, setEntry] = useState(() => ({ location: route, token: ['/join', '/share'].includes(route.pathname) ? incomingToken() : null, generation: 0 }));
  // A new link is an explicit new flow. Reset before committing the old link's children.
  if (['/join', '/share'].includes(route.pathname) && route.hash && entry.location !== route) {
    setEntry({ location: route, token: incomingToken(), generation: entry.generation + 1 });
  }
  useLayoutEffect(() => {
    if (!['/join', '/share'].includes(route.pathname) || !route.hash) return;
    // A newer native fragment can arrive before this commit; let Router capture it first.
    if (window.location.hash && window.location.hash !== route.hash) return;
    if (window.location.hash) window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}`);
    // Keep Router's location scrubbed too, so a later identical native hash is a new entry.
    if (route.hash) void navigate({ pathname: route.pathname, search: route.search, hash: '' }, { replace: true, preventScrollReset: true });
  }, [route, navigate]);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ attempt: number; enabled?: boolean }>({ attempt: -1 });
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch('/api/v1/features', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error('features unavailable');
        const payload: unknown = await response.json();
        if (!payload || typeof payload !== 'object' || !('tactics_enabled' in payload) || typeof payload.tactics_enabled !== 'boolean') throw new Error('invalid features');
        if (!controller.signal.aborted) setResult({ attempt, enabled: payload.tactics_enabled });
      } catch {
        if (!controller.signal.aborted) setResult({ attempt });
      }
    }
    void load();
    return () => controller.abort();
  }, [attempt]);
  if (result.attempt === attempt && result.enabled === true) return publicShare ? <SharePage key={entry.generation} token={entry.token} /> : <AccountPage key={route.pathname === '/join' ? entry.generation : 'account'} inviteToken={entry.token} />;
  return <main className="page-shell compact-page"><h1>个人战术本</h1>
    {result.attempt !== attempt ? <p role="status">正在确认战术本是否开放…</p> : result.enabled === false ? <p>战术本暂未开放，请稍后再来。</p> : <><p role="alert">暂时无法确认战术本是否开放，请重试。</p><button className="button" onClick={() => setAttempt(value => value + 1)}>重试</button></>}
    <Link to="/">返回教学页</Link>
  </main>;
}
