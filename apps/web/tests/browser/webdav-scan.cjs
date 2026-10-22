/* eslint-disable @typescript-eslint/no-require-imports -- Synthetic isolated browser regression. */
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');
let scan = null, starts = 0, cancels = 0, resumes = 0;
let failList = false;
const source = () => ({ id: 1, name: '合成 WebDAV', base_url: 'https://dav.example/', root_path: '/docs',
  username: 'demo', is_enabled: true, recursive: true, include_extensions: ['.md'], ignore_patterns: [],
  sync_status: scan && ['queued', 'running'].includes(scan.status) ? 'scanning' : 'idle',
  entry_counts: { total: 0, pending: 0, synced: 0, failed: 0, missing: 0, suspected_missing: 0 }, scan });
const fixture = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture.test');
  const json = (body, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
  if (url.pathname === '/api/auth/status') return json({ authenticated: true, setup_required: false, admin: { id: 1, username: 'demo' } });
  if (url.pathname === '/api/workspaces/current') return json({ slug: 'default', name: '合成空间', status: 'active', settings: {} });
  if (url.pathname === '/api/workspaces') return json([{ slug: 'default', name: '合成空间', status: 'active', settings: {} }]);
  if (url.pathname === '/api/webdav') return failList ? json({ detail: 'synthetic outage' }, 503) : json([source()]);
  if (url.pathname.endsWith('/scan-jobs/cancel')) { cancels++; scan = { ...scan, status: 'cancelled' }; return json(scan); }
  if (url.pathname.endsWith('/scan-jobs') && req.method === 'POST') {
    if (url.searchParams.has('resume')) { resumes++; scan = { ...scan, status: 'queued', resumed: true }; }
    else { starts++; scan = { id: starts, status: 'queued', scanned_directories: 0, pending_directories: 1, discovered_files: 0, elapsed_seconds: 0, resumed: false, error: null, result: null }; }
    return json(scan, 202);
  }
  if (url.pathname.endsWith('/entries')) return json([]);
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
  let log = ''; server.stdout.on('data', data => { log += data; }); server.stderr.on('data', data => { log += data; });
  let browser;
  try {
    for (let i = 0; i < 120; i++) {
      try { if ((await fetch(`${base}/api/auth/status`)).ok) break; } catch { /* startup */ }
      await delay(250);
    }
    browser = await chromium.launch({ headless: true,
      executablePath: process.env.CANGZHI_TEST_CHROMIUM || ['/usr/bin/google-chrome', '/usr/bin/chromium'].find(file => fs.existsSync(file)) });
    const page = await browser.newPage(); const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await page.goto(`${base}/settings/sources`);
    const button = label => page.getByRole('button', { name: label, exact: true });
    await button('扫描文件').click();
    await page.getByText('等待后台扫描', { exact: true }).waitFor();
    assert.equal(starts, 1);
    assert.equal(await button('扫描文件').isDisabled(), true);
    assert.equal(await button('同步入库').isDisabled(), true);
    scan = { ...scan, status: 'running', scanned_directories: 10, pending_directories: 3, discovered_files: 26, elapsed_seconds: 8 };
    await page.getByText(/已扫描 10 个目录/).waitFor();
    failList = true;
    await page.getByText(/扫描进度暂时无法刷新/).waitFor();
    failList = false;
    await page.getByText(/扫描进度暂时无法刷新/).waitFor({ state: 'hidden' });
    // Remount the source panel: progress comes from the server, not component state.
    await page.getByRole('tab', { name: /数据库来源/ }).click();
    await page.getByRole('tab', { name: /文件来源/ }).click();
    await page.getByText(/已扫描 10 个目录/).waitFor();
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      const progress = page.getByRole('status').filter({ hasText: '正在扫描目录' });
      assert.equal(await progress.evaluate(el => el.scrollWidth <= el.clientWidth + 1), true);
      await button('取消扫描').scrollIntoViewIfNeeded();
    }
    await button('取消扫描').click();
    await page.getByText('扫描已取消', { exact: true }).waitFor();
    assert.equal(cancels, 1);
    await button('从断点继续').click();
    await page.getByText(/这是断点继续的扫描/).waitFor();
    assert.equal(resumes, 1);
    scan = { ...scan, status: 'failed', error: '合成连接失败' };
    await page.getByText('扫描中断', { exact: true }).waitFor();
    await button('从断点继续').click();
    await page.getByText('等待后台扫描', { exact: true }).waitFor();
    scan = { ...scan, status: 'completed', error: null, pending_directories: 0,
      result: { eligible_files: 26, missing_check_performed: false } };
    await page.getByText('扫描完成', { exact: true }).waitFor();
    assert.equal(await button('同步入库').isDisabled(), false);
    await page.getByText(/点击“同步入库”才会下载并处理/).waitFor();
    assert.deepEqual(errors, []);
    process.stdout.write('PASS background enqueue, polling, remount restoration, cancel/resume/failure/completion and mobile/desktop progress; synthetic API only\n');
  } catch (error) { process.stderr.write(log); throw error; }
  finally { await browser?.close(); server.kill('SIGTERM'); await new Promise(resolve => fixture.close(resolve)); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
