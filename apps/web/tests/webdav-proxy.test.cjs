/* eslint-disable @typescript-eslint/no-require-imports -- Node HTTP regression. */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { syncBatchPath, forwardUpload } = require('../server.cjs');

test('only bounded import path opts into scoped timeout, including base path', () => {
  assert.equal(syncBatchPath('/api/webdav/12/sync-batch', ''), '/api/webdav/12/sync-batch');
  assert.equal(syncBatchPath('/kb/api/webdav/12/sync-batch', '/kb'), '/api/webdav/12/sync-batch');
  for (const path of ['/api/webdav/12/sync', '/api/webdav/12/sync-batch/other', '/api/v1/knowledge/ask']) {
    assert.equal(syncBatchPath(path, ''), null);
  }
});

const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const close = server => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); });

test('bounded proxy preserves auth/body/workspace and upstream status', async () => {
  const upstream = http.createServer((req, res) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      assert.equal(req.headers.cookie, 'synthetic-cookie');
      assert.equal(req.headers['x-cangzhi-workspace'], 'isolated');
      assert.deepEqual(JSON.parse(body), { entry_ids: [4], limit: 1 });
      res.writeHead(409, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ detail: 'busy' }));
    });
  });
  const apiPort = await listen(upstream);
  const proxy = http.createServer((req, res) => forwardUpload(req, res, `http://127.0.0.1:${apiPort}`, '/api/webdav/1/sync-batch', { timeoutMs: 1000 }));
  const port = await listen(proxy);
  try {
    const response = await fetch(`http://127.0.0.1:${port}`, { method: 'POST', headers: {
      cookie: 'synthetic-cookie', 'X-Cangzhi-Workspace': 'isolated', 'Content-Type': 'application/json',
    }, body: JSON.stringify({ entry_ids: [4], limit: 1 }) });
    assert.equal(response.status, 409);
    assert.equal((await response.json()).detail, 'busy');
  } finally { await close(proxy); await close(upstream); }
});

test('bounded proxy timeout reports uncertainty rather than whole-batch rollback', async () => {
  const upstream = http.createServer(() => {});
  const apiPort = await listen(upstream);
  const proxy = http.createServer((req, res) => forwardUpload(req, res, `http://127.0.0.1:${apiPort}`, '/api/webdav/1/sync-batch', {
    timeoutMs: 30, failureMessage: '请刷新清单确认结果',
  }));
  const port = await listen(proxy);
  try {
    const response = await fetch(`http://127.0.0.1:${port}`, { method: 'POST' });
    assert.equal(response.status, 502);
    assert.equal((await response.json()).detail, '请刷新清单确认结果');
  } finally { await close(proxy); await close(upstream); }
});

test('bounded proxy rejects an oversized JSON selection before forwarding', async () => {
  let requests = 0;
  const upstream = http.createServer((_req, res) => { requests++; res.end('{}'); });
  const apiPort = await listen(upstream);
  const proxy = http.createServer((req, res) => forwardUpload(req, res, `http://127.0.0.1:${apiPort}`, '/api/webdav/1/sync-batch', { maxBytes: 64 }));
  const port = await listen(proxy);
  try {
    const response = await fetch(`http://127.0.0.1:${port}`, { method: 'POST', body: 'x'.repeat(65) });
    assert.equal(response.status, 413);
    assert.equal(requests, 0);
  } finally { await close(proxy); await close(upstream); }
});
