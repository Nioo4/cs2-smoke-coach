// New-fixture-only operator tool. Never imported by the running collaboration server.
import { randomUUID } from 'node:crypto';
import { open, realpath } from 'node:fs/promises';
import { basename, dirname, resolve } from 'node:path';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';

export const ORIGIN = 'http://127.0.0.1:18220';
// Public disposable fixture value, intentionally not a real account secret.
const FIXTURE_PASSWORD = 'tactics-web-fixture-public-2026';
const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
class FixtureError extends Error {}
const requireCheck = (condition, code) => { if (!condition) throw new FixtureError(code); };

export function options(args) {
  const parsed = {};
  for (let index = 0; index < args.length; index++) {
    const arg = args[index];
    requireCheck(!(arg in parsed), 'DUPLICATE_ARGUMENT');
    if (arg === '--execute') parsed[arg] = true;
    else {
      requireCheck(arg === '--output' && args[index + 1] && !args[index + 1].startsWith('--'), 'INVALID_ARGUMENT');
      parsed[arg] = args[++index];
    }
  }
  if (!parsed['--execute']) return { execute: false };
  requireCheck(typeof parsed['--output'] === 'string', 'NEW_OUTPUT_REQUIRED');
  const path = resolve(ROOT, parsed['--output']);
  requireCheck(dirname(path) === resolve(ROOT, '.workcache/t5') && /^web-fixture-[a-z0-9-]{1,64}\.json$/.test(basename(path)), 'OUTPUT_BOUNDARY_REJECTED');
  return { execute: true, path };
}

export async function main(args = process.argv.slice(2)) {
  let output; let state;
  try {
    const args_ = options(args);
    if (!args_.execute) { process.stdout.write('DRY_RUN_NO_REQUESTS: fixed 18220, new account, 21 tactics, 2 teams, no invites\n'); return; }
    const directory = await realpath(dirname(args_.path));
    requireCheck(directory === resolve(ROOT, '.workcache/t5'), 'OUTPUT_DIRECTORY_REJECTED');
    // Reserve the new artifact BEFORE any network/write operation; never open an existing output.
    output = await open(args_.path, 'wx', 0o600);
    state = { format_version: 1, origin: ORIGIN, status: 'PREPARED', started_at: new Date().toISOString(),
      username: `web_${randomUUID().replaceAll('-', '').slice(0, 20)}`, user_id: null,
      personal_notebook_id: null, tactic_ids: [], teams: [],
      counts: { accounts: 0, tactics: 0, teams: 0 }, stage: 'FEATURES', failure_code: null };
    const checkpoint = async () => {
      // Only this safe state is serialized; credential locals never enter it.
      const bytes = Buffer.from(JSON.stringify(state, null, 2) + '\n');
      const written = await output.write(bytes, 0, bytes.length, 0);
      requireCheck(written.bytesWritten === bytes.length, 'OUTPUT_WRITE_INCOMPLETE');
      await output.truncate(bytes.length); await output.sync();
    };
    await checkpoint();
    let cookie = ''; let csrf = ''; let lastStart = -Infinity; let writePending = false;
    const request = async (path, { body, status = 200 } = {}) => {
      const method = body === undefined ? 'GET' : 'POST';
      // Every request is awaited. No parallel register, queue, automatic retry or redirect follow.
      while (performance.now() - lastStart < 650) await sleep(Math.max(1, 650 - (performance.now() - lastStart)));
      lastStart = performance.now(); writePending = method === 'POST';
      let response;
      try {
        response = await fetch(`${ORIGIN}/api/v1${path}`, { method, redirect: 'error', signal: AbortSignal.timeout(30_000),
          headers: { origin: ORIGIN, ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
            ...(body === undefined ? {} : { 'content-type': 'application/json' }),
            ...(method === 'POST' && path !== '/auth/register' ? { 'idempotency-key': randomUUID() } : {}) },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      } catch { throw new FixtureError(writePending ? 'WRITE_OUTCOME_UNKNOWN' : 'READ_UNAVAILABLE'); }
      requireCheck(response.status === status, `HTTP_${response.status}_STOPPED`);
      let data;
      try { data = await response.json(); } catch { throw new FixtureError(writePending ? 'WRITE_RESPONSE_UNKNOWN' : 'READ_RESPONSE_INVALID'); }
      return { data, response };
    };
    try {
      const features = (await request('/features')).data;
      requireCheck(features?.tactics_enabled === true, 'FEATURE_DISABLED_OR_INVALID');
      state.stage = 'REGISTER'; await checkpoint();
      const registered = await request('/auth/register', { status: 201, body: { username: state.username,
        display_name: '网页分页与邀请fixture', password: FIXTURE_PASSWORD } });
      requireCheck(UUID.test(registered.data?.user?.id) && registered.data.user.username === state.username, 'REGISTER_RESPONSE_UNKNOWN');
      state.user_id = registered.data.user.id; state.counts.accounts = 1; await checkpoint();
      const cookies = registered.response.headers.getSetCookie().filter(value => value.startsWith('smokeline_session='));
      requireCheck(cookies.length === 1 && typeof registered.data.csrf_token === 'string' && registered.data.csrf_token.length > 0, 'REGISTER_SESSION_UNCONFIRMED');
      cookie = cookies[0].split(';')[0]; csrf = registered.data.csrf_token;
      state.stage = 'PERSONAL_NOTEBOOK'; await checkpoint();
      const notebook = (await request('/notebooks/personal')).data?.notebook;
      requireCheck(UUID.test(notebook?.id) && notebook.kind === 'personal' && notebook.user_id === state.user_id, 'NOTEBOOK_IDENTITY_INVALID');
      state.personal_notebook_id = notebook.id; await checkpoint();
      for (let index = 1; index <= 21; index++) {
        state.stage = `TACTIC_${index}`; await checkpoint();
        const created = (await request(`/notebooks/${notebook.id}/tactics`, { status: 201,
          body: { title: `网页分页 ${String(index).padStart(2, '0')}/21`, side: 'T' } })).data;
        const tactic = created?.tactic;
        requireCheck(UUID.test(tactic?.id) && tactic.notebook_id === notebook.id && created?.projection?.structure?.document_id === tactic.id && !state.tactic_ids.includes(tactic.id), 'TACTIC_RESPONSE_UNKNOWN');
        state.tactic_ids.push(tactic.id); state.counts.tactics++; await checkpoint();
      }
      for (let index = 1; index <= 2; index++) {
        state.stage = `TEAM_${index}`; await checkpoint();
        const created = (await request('/teams', { status: 201,
          body: { name: `同路径邀请队伍 ${index}` } })).data;
        const team = created?.team; const book = created?.notebook;
        requireCheck(UUID.test(team?.id) && UUID.test(book?.id) && team.owner_id === state.user_id && team.notebook_id === book.id && book.team_id === team.id && !state.teams.some(item => item.id === team.id), 'TEAM_RESPONSE_UNKNOWN');
        state.teams.push({ id: team.id, notebook_id: book.id }); state.counts.teams++; await checkpoint();
      }
      state.stage = 'COMPLETE'; state.status = 'COMPLETE'; state.finished_at = new Date().toISOString(); await checkpoint();
    } catch (error) {
      state.status = 'STOPPED_NO_RETRY'; state.failure_code = error instanceof FixtureError ? error.message : writePending ? 'WRITE_OUTCOME_UNKNOWN' : 'UNEXPECTED_FAILURE_STOPPED';
      await checkpoint(); process.exitCode = 1;
    }
    process.stdout.write(JSON.stringify({ status: state.status, output: args_.path, counts: state.counts }) + '\n');
  } catch (error) {
    process.exitCode = 1;
    process.stderr.write(error?.code === 'EEXIST' ? 'OUTPUT_EXISTS_NO_REQUESTS\n' : 'FIXTURE_PREFLIGHT_OR_OUTPUT_FAILED_NO_RETRY\n');
  } finally { await output?.close(); }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
