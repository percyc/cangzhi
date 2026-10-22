/* eslint-disable @typescript-eslint/no-require-imports -- Isolated synthetic browser regression. */
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');
let empty = false, fail = false, calls = 0;
const section = data => ({ data, sampled_at: data ? new Date().toISOString() : null, stale: !data, error: null });
const fixture = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture.test');
  const json = (body, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
  if (url.pathname === '/api/auth/status') return json({ authenticated: true, setup_required: false, admin: { username: 'demo' } });
  if (url.pathname === '/api/workspaces/current') return json({ slug: 'default', name: '合成空间', status: 'active' });
  if (url.pathname === '/api/workspaces') return json([{ slug: 'default', name: '合成空间', status: 'active' }]);
  if (url.pathname === '/api/system/overview') {
    calls++;
    if (fail) return json({}, 503);
    return json({ sections: {
      knowledge: section(empty ? null : { documents: 1234, files: 1000, chunks: 45678, trash: 2, versions: 1400, parent_chunks: 200, datasets: 8, active_vectors: 300, vectors: 600 }),
      queue: { ...section(empty ? null : { waiting: 12, running: 1, failed: 2, completed: 800, oldest_waiting_at: null }), stale: !empty, error: empty ? null : '采集暂不可用，保留上次结果' },
      capacity: section(empty ? null : { disk: { total: 100000, used: 95000, free: 5000 }, blob_bytes: 3000, original_bytes: 2000, preview_bytes: 1000, other_blob_bytes: 0, parquet_bytes: 40, database_bytes: 100, vector_table_bytes: 20, blobs: 40 }),
    }, history: empty ? [] : [1, 2, 3].map(n => ({ at: new Date(Date.now() - (4-n)*3600000).toISOString(), documents: n*100, blob_bytes: n*1000 })) });
  }
  return json([]);
});
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function main() {
  const apiPort = await listen(fixture);
  const reservation = http.createServer(); const port = await listen(reservation);
  await new Promise(resolve => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(process.execPath, [require.resolve('next/dist/bin/next'), 'dev', '--hostname', '127.0.0.1', '--port', String(port)], {
    cwd: path.join(__dirname, '../..'), env: { ...process.env, API_URL: `http://127.0.0.1:${apiPort}`, NEXT_TELEMETRY_DISABLED: '1', CANGZHI_WEB_BASE_PATH: '' }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '', browser;
  server.stdout.on('data', b => { log += b; }); server.stderr.on('data', b => { log += b; });
  try {
    for (let i=0;i<120;i++) { try { if ((await fetch(`${base}/api/auth/status`)).ok) break; } catch {} await delay(250); }
    browser = await chromium.launch({ headless: true, executablePath: ['/usr/bin/google-chrome', '/usr/bin/chromium'].find(p => fs.existsSync(p)) });
    const page = await browser.newPage(); const errors = [];
    page.on('pageerror', err => errors.push(err.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await page.goto(`${base}/settings/overview`);
    await page.getByText('1,234', { exact: true }).first().waitFor();
    await page.getByText(/数据盘已使用 95/).waitFor();
    await page.getByText(/数据已过期/).waitFor();
    await page.getByRole('img', { name: '已登记文件容量趋势' }).waitFor();
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    }
    await page.getByLabel('趋势时间范围').selectOption('30');
    fail = true;
    await page.getByRole('button', { name: '重新读取快照' }).click();
    await page.getByRole('alert').filter({ hasText: '暂时无法读取' }).waitFor();
    assert.equal(await page.getByText('1,234', { exact: true }).count(), 2);
    fail = false; empty = true;
    await page.getByRole('button', { name: '重新读取快照' }).click();
    await page.getByText(/等待后台首次采集/).first().waitFor();
    await page.getByText(/趋势正在积累/).waitFor();
    assert.ok(calls < 12); assert.deepEqual(errors, []);
    console.log('PASS monitoring snapshots, alerts, stale/error/empty states, history selection and responsive layouts');
  } catch (err) { process.stderr.write(log); throw err; }
  finally { await browser?.close(); server.kill('SIGTERM'); await new Promise(resolve => fixture.close(resolve)); }
}
main().catch(err => { console.error(err); process.exitCode = 1; });
