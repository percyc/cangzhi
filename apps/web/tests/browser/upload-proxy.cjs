/* eslint-disable @typescript-eslint/no-require-imports -- Isolated Node HTTP integration harness. */
// Production Next.js + synthetic upstream only: no credentials, database or real uploads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { createHash } = require('node:crypto');
const { spawn } = require('node:child_process');
const { once } = require('node:events');

const web = path.resolve(__dirname, '../..');
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'cangzhi-upload-proxy-'));
const MiB = 1024 * 1024;
const prefix = '/_upload_test';
const head = Buffer.from('--synthetic\r\nContent-Disposition: form-data; name="file"; filename="test.docx"\r\nContent-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n');
const tail = Buffer.from('\r\n--synthetic--\r\n');
let next;
let logs = '';
const upstream = http.createServer(async (req, res) => {
  const hash = createHash('sha256');
  let bytes = 0;
  for await (const chunk of req) { bytes += chunk.length; hash.update(chunk); }
  const status = req.headers.authorization !== 'Bearer synthetic-test'
    ? 401 : bytes - head.length - tail.length > 50 * MiB ? 413 : 201;
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ bytes, hash: hash.digest('hex'), route: req.url }));
});

async function main() {
  // Copy source only. Never copy environment files, build outputs or user data.
  for (const entry of fs.readdirSync(web)) {
    if (['node_modules', '.next', 'tests', 'tsconfig.tsbuildinfo'].includes(entry) || entry.startsWith('.env')) continue;
    fs.cpSync(path.join(web, entry), path.join(scratch, entry), { recursive: true });
  }
  fs.symlinkSync(path.join(web, 'node_modules'), path.join(scratch, 'node_modules'), 'dir');
  upstream.listen(0, '127.0.0.1');
  await once(upstream, 'listening');
  const env = { ...process.env, NEXT_TELEMETRY_DISABLED: '1', MAX_UPLOAD_SIZE_MB: '50',
    API_URL: `http://127.0.0.1:${upstream.address().port}`, CANGZHI_WEB_BASE_PATH: prefix };
  const cli = path.join(web, 'node_modules/next/dist/bin/next');
  const build = spawn(process.execPath, [cli, 'build', '--webpack'], { cwd: scratch, env, stdio: 'inherit' });
  assert.equal((await once(build, 'exit'))[0], 0, 'production build');
  const built = JSON.parse(fs.readFileSync(path.join(scratch, '.next/required-server-files.json')));
  assert.equal(built.config.experimental.proxyClientMaxBodySize, 51 * MiB);

  // Allocate a local test port; do not bind the running application's ports.
  const probe = http.createServer();
  probe.listen(0, '127.0.0.1');
  await once(probe, 'listening');
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  next = spawn(process.execPath, [cli, 'start', '-H', '127.0.0.1', '-p', String(port)], { cwd: scratch, env });
  for (const stream of [next.stdout, next.stderr]) stream.on('data', chunk => { logs += chunk.toString(); });
  const origin = `http://127.0.0.1:${port}`;
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (next.exitCode !== null) throw new Error(logs);
    try { ready = (await fetch(`${origin}${prefix}/api/health`)).ok; } catch { /* starting */ }
    if (ready) break;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  assert.ok(ready, 'isolated Web ready');
  for (const [route, size, authorized, status] of [
    ['/api/files/upload', 11 * MiB, true, 201],
    [`${prefix}/api/files/upload`, 50 * MiB, true, 201],
    [`${prefix}/api/files/upload`, 50 * MiB + 1, true, 413],
    ['/api/files/upload', 11 * MiB, false, 401],
    [`${prefix}/api/upload`, 11 * MiB, true, 201],
  ]) {
    const body = Buffer.concat([head, Buffer.alloc(size, 120), tail]);
    const hash = createHash('sha256').update(body).digest('hex');
    const response = await fetch(`${origin}${route}`, { method: 'POST', body,
      signal: AbortSignal.timeout(30000), headers: {
        'Content-Type': 'multipart/form-data; boundary=synthetic',
        ...(authorized ? { Authorization: 'Bearer synthetic-test' } : {}),
      } });
    assert.equal(response.status, status);
    const received = await response.json();
    assert.equal(received.bytes, body.length, 'upstream receives entire multipart request');
    assert.equal(received.hash, hash, 'upstream content hash is identical');
    assert.equal(received.route, route.replace(prefix, ''));
    console.log(`PASS ${route}: file=${size} bytes, status=${status}, hash intact`);
  }
  assert.doesNotMatch(logs, /Request body exceeded|ECONNRESET/);
}

main().catch(error => { console.error(error, logs); process.exitCode = 1; }).finally(async () => {
  if (next && next.exitCode === null) {
    next.kill('SIGTERM');
    await once(next, 'exit');
  }
  upstream.closeAllConnections();
  await new Promise(resolve => upstream.close(resolve));
  fs.rmSync(scratch, { recursive: true, force: true });
});
