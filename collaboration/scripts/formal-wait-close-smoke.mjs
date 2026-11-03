import WebSocket from 'ws';

const socket = new WebSocket(process.env.FORMAL_TEST_WS, { origin: process.env.FORMAL_TEST_ORIGIN });
const timeout = setTimeout(() => { process.stderr.write('close_timeout\n'); process.exitCode = 1; socket.terminate(); }, 7000);
socket.on('open', () => socket.send(JSON.stringify({ type: 'auth', ticket: process.env.FORMAL_TEST_TICKET })));
socket.on('message', (raw) => { if (JSON.parse(raw.toString()).type === 'sync') process.stdout.write('CONNECTED\n'); });
socket.on('close', (code) => { clearTimeout(timeout); process.stdout.write(`CLOSED ${code}\n`); });
socket.on('error', () => { clearTimeout(timeout); process.exitCode = 1; });
