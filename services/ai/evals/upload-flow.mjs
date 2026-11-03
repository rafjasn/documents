// Uploads files the way the frontend does and prints the status events the browser
// receives over Socket.IO, then the document as the API returns it.
//
// Needs the core stack (docker compose up). From the repository root:
//   node services/ai/evals/upload-flow.mjs <file>...
// Uses the account FLOW_CHECK_EMAIL (default flow-check@documents.local), registering it
// on first use with FLOW_CHECK_PASSWORD.
import { readFile } from 'node:fs/promises';
import { basename, extname } from 'node:path';
import { io } from 'socket.io-client';

const API = process.env.API_URL ?? 'http://localhost';
const WS = process.env.WS_URL ?? 'ws://localhost';
const EMAIL = process.env.FLOW_CHECK_EMAIL ?? 'flow-check@documents.local';
const PASSWORD = process.env.FLOW_CHECK_PASSWORD ?? 'flow-check-password';
const TIMEOUT_MS = 10 * 60 * 1000;
const TYPES = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.txt': 'text/plain',
    '.csv': 'text/csv'
};

async function post(path, body, token) {
    const response = await fetch(`${API}/api${path}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {})
        },
        body: JSON.stringify(body)
    });
    return { ok: response.ok, status: response.status, body: await response.json() };
}

async function login() {
    const credentials = { email: EMAIL, password: PASSWORD };
    let result = await post('/auth/login', credentials);
    if (!result.ok) {
        await post('/auth/register', credentials);
        result = await post('/auth/login', credentials);
    }
    if (!result.ok) throw new Error(`Login failed: ${result.status} ${JSON.stringify(result.body)}`);
    return result.body;
}

const files = process.argv.slice(2);
if (files.length === 0) {
    console.error('Usage: node services/ai/evals/upload-flow.mjs <file>...');
    process.exit(2);
}

const { accessToken, userId } = await login();
console.log(`Signed in as ${EMAIL} (${userId})`);
const socket = io(`${WS}/documents`, { auth: { token: accessToken }, transports: ['websocket'] });
await new Promise((resolve, reject) => {
    socket.on('connect', resolve);
    socket.on('connect_error', reject);
});

const waiting = new Map(); // documentId -> { events, done }
socket.on('document:status', (event) => {
    const entry = waiting.get(event.documentId);
    if (!entry) return;
    entry.events.push({
        status: event.status,
        fields: Object.keys(event.document ?? {}),
        seconds: ((Date.now() - entry.started) / 1000).toFixed(1)
    });
    if (event.status === 'COMPLETED' || event.status === 'FAILED') entry.done();
});

let failures = 0;
for (const file of files) {
    const mimeType = TYPES[extname(file).toLowerCase()];
    const data = await readFile(file);
    const presign = await post(
        '/documents/presign',
        { fileName: basename(file), mimeType, fileSize: data.length },
        accessToken
    );
    if (!presign.ok) throw new Error(`Presign failed: ${JSON.stringify(presign.body)}`);
    const { documentId, uploadUrl, fields } = presign.body;

    let done;
    const finished = new Promise((resolve) => (done = resolve));
    const entry = { events: [], started: Date.now(), done };
    waiting.set(documentId, entry);

    const form = new FormData();
    for (const [key, value] of Object.entries(fields)) form.append(key, value);
    form.append('file', new Blob([data], { type: mimeType }), basename(file));
    const upload = await fetch(uploadUrl, { method: 'POST', body: form });
    if (!upload.ok) throw new Error(`S3 upload failed: ${upload.status}`);

    const timer = setTimeout(done, TIMEOUT_MS);
    await finished;
    clearTimeout(timer);

    const record = await (
        await fetch(`${API}/api/documents/${documentId}`, {
            headers: { Authorization: `Bearer ${accessToken}` }
        })
    ).json();
    const last = entry.events.at(-1)?.status;
    if (last !== 'COMPLETED') failures += 1;
    console.log(`\n${basename(file)} (${mimeType})`);
    for (const event of entry.events) {
        const extra = event.fields.length ? ` {${event.fields.join(', ')}}` : '';
        console.log(`  ${event.seconds.padStart(6)} s  ${event.status}${extra}`);
    }
    console.log(
        `  API: status ${record.status}, category ${record.category}, ` +
            `language ${record.metadata?.language}, thumbnail ${record.thumbnailUrl ? 'yes' : 'no'}`
    );
    console.log(`  summary: ${(record.summary ?? record.errorMessage ?? '').slice(0, 160)}`);
}

socket.close();
process.exit(failures ? 1 : 0);
