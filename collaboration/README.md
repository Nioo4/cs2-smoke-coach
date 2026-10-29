# SmokeLine Tactics T0

T0 is a local-only collaboration prototype for shared text and map-object leases. Its fixture IDs and example content are implementation-only, not the formal UUID or published-content contract. It reuses the repository's Mirage SVG, but the text, steps, and markers are non-game validation data.

## Run locally

Requirements: Node.js 24 and Docker Compose. From this directory:

```powershell
npm ci
docker compose -f compose.t0.yml up -d postgres
npm run build
$env:TACTICS_T0_MODE = 'local'
$env:DATABASE_URL = 'postgresql://t0_test:t0_only_local@127.0.0.1:55432/t0_tactics'
npm start
```

Open `http://127.0.0.1:4173/`. In another local tab, issue the two one-use participant tickets with:

```powershell
$tickets = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:4173/__t0/test-tickets -Headers @{ Origin = 'http://127.0.0.1:4173' }
$tickets.participants
```

Paste a different participant token into each tab. Tickets expire after five minutes and are single-use. Stop the app with Ctrl+C and stop only this database service with `docker compose -f compose.t0.yml stop postgres`; the named volume is intentionally retained for restart-recovery checks.

The stage-based dynamic board is at `http://127.0.0.1:4173/stage-board.html` and persists to the separate `mirage-stage-prototype` document. Issue its tickets by explicitly selecting that allowlisted document:

```powershell
$stageTickets = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:4173/__t0/test-tickets -Headers @{ Origin = 'http://127.0.0.1:4173' } -ContentType 'application/json' -Body '{"document_id":"mirage-stage-prototype"}'
$stageTickets.participants
```

Paste a different token into each local browser tab. The old `/` entry and its `mirage-prototype` data remain independent; the dynamic board does not migrate or overwrite them.

Text undo/redo buttons and Ctrl/Cmd+Z / Ctrl/Cmd+Shift+Z affect only edits made by the current connection; remote edits are not added to this undo history, and undo/redo changes still save through the normal collaboration operation. Text editing and undo/redo are disabled while disconnected, including after switching steps. Leaving the page prompts while edits are pending, rejected, in composition, or preserved as recovery data; exporting a copy does not mark that content saved or dismiss the warning.

If Docker cannot allocate a default network because its address pool is exhausted, inspect existing networks and choose a free local subnet before creating an untracked Compose override. Keep that override in ignored local workspace data and pass it as an additional `-f` file. Do not bake a machine-specific subnet into `compose.t0.yml`.

## Checks

```powershell
npm run lint
npm test
npm run build
npm run test:pg
```

For the repeatable live boundary audit against the local prototype, run `node scripts/live-boundary-audit.mjs` or pass an explicit `http://127.0.0.1` prototype URL. The script refuses non-loopback/non-HTTP targets and checks the local prototype health marker first. It verifies ticket and WebSocket Origin rejection, single-use ticket behavior, and rejection of a text update that adds an extra Yjs root; the negative update must not produce `operation.applied`, change revision, or alter the resynchronized document. It does not submit a valid text or map write. Output omits ticket values and document text. Do not use it against production or any non-local environment.

`test:pg` requires `TACTICS_T0_DATABASE_URL` or `DATABASE_URL` to point only at the isolated T0 PostgreSQL database. It starts separate writer and reader Node processes and verifies that text persisted by one process is recovered by another. It also holds a row lock in a unique legacy-test document to exercise the PostgreSQL statement timeout and verifies that a timed-out save emits no success acknowledgement while the local pending edit remains available. Finally, it uses a unique `t0-stage-test-*` fixture to persist a dynamic-board action through `stage.command`, then reloads it through a fresh collaboration core and verifies the structure, description, and revision. Unique test fixtures are retained in the isolated database; none use either visible prototype document. These are local technical checks, not production acceptance. The pool uses bounded connection/query/statement/idle-transaction waits and records only a safe error code and timestamp for idle-client errors.
