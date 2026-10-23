/* eslint-disable @typescript-eslint/no-require-imports -- Isolated HTTP transport acceptance. */
const assert = require('node:assert/strict');
const http = require('node:http');
const { spawn } = require('node:child_process');
const path = require('node:path');
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const fixture = http.createServer((req, res) => {
    req.resume();
    if (req.url === '/api/auth/status') {
      res.setHeader('Content-Type', 'application/json');
      return res.end(JSON.stringify({ authenticated: true, setup_required: false }));
    }
    if (req.url !== '/api/v1/knowledge/ask/stream') { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'Content-Type': 'application/x-ndjson', 'Cache-Control': 'no-cache' });
    const progress = () => res.write(JSON.stringify({ type: 'progress', message: '合成慢请求仍在运行' }) + '\n');
    progress();
    const heartbeat = setInterval(progress, 5000);
    const finish = setTimeout(() => res.end(JSON.stringify({ type: 'result', data: { answer: 'synthetic answer' } }) + '\n'), 32000);
    res.on('close', () => { clearInterval(heartbeat); clearTimeout(finish); });
  });
  const apiPort = await listen(fixture);
  const reservation = http.createServer();
  const port = await listen(reservation);
  await new Promise(resolve => reservation.close(resolve));
  const server = spawn(process.execPath, ['server.cjs', '--dev', '--hostname', '127.0.0.1', '--port', String(port)], {
    cwd: path.resolve(__dirname, '../..'),
    env: { ...process.env, API_URL: `http://127.0.0.1:${apiPort}`, CANGZHI_WEB_BASE_PATH: '', NEXT_TELEMETRY_DISABLED: '1' },
    stdio: ['ignore', 'ignore', 'ignore'],
  });
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { ready = (await fetch(`http://127.0.0.1:${port}/api/auth/status`)).ok; } catch { /* startup */ }
      if (ready) break;
      await delay(250);
    }
    assert.ok(ready, 'isolated web did not start');
    const start = Date.now();
    const response = await fetch(`http://127.0.0.1:${port}/api/v1/knowledge/ask/stream`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: 'synthetic slow question', mode: 'quick' }),
      signal: AbortSignal.timeout(45000),
    });
    assert.equal(response.status, 200);
    assert.ok(Date.now() - start < 10000, 'headers must arrive before the final result');
    const events = (await response.text()).trim().split('\n').map(line => JSON.parse(line));
    assert.ok(Date.now() - start >= 30000);
    assert.ok(events.filter(event => event.type === 'progress').length >= 5);
    assert.equal(events.at(-1).data.answer, 'synthetic answer');
    console.log('PASS real Next proxy: quick NDJSON heartbeats and final result survive 32 seconds');
  } finally {
    server.kill('SIGTERM');
    await new Promise(resolve => server.exitCode !== null ? resolve() : server.once('exit', resolve));
    fixture.closeAllConnections();
    await new Promise(resolve => fixture.close(resolve));
  }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
