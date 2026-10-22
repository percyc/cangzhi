/* eslint-disable @typescript-eslint/no-require-imports -- Isolated synthetic browser regression. */
// No deployed API, credentials, real WebDAV server, documents or model requests.
// The synthetic fixture implements the browse/sync-batch contract so pagination,
// folder drill-down, selection payloads, partial-failure progress, stop without a
// runaway loop and mobile overflow are exercised through the real component.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

const ROOT = '/docs';
const files = [
  { id: 1, dir: '/docs', name: 'root-a.md', state: 'discovered', size: 1024 },
  { id: 2, dir: '/docs', name: 'root-b.md', state: 'synced', size: 2048 },
  { id: 3, dir: '/docs/team', name: 'team-c.md', state: 'discovered', size: 512 },
  { id: 4, dir: '/docs/team', name: 'team-d.md', state: 'failed', size: 256, last_error: '合成远端读取失败' },
  { id: 5, dir: '/docs/team/deep', name: 'deep-e.md', state: 'changed', size: 128 },
  { id: 6, dir: '/docs/team', name: 'team-ignored.md', state: 'ignored', size: 64, ignore_reason: 'permanent_deleted' },
  { id: 7, dir: '/docs/other', name: 'other-f.md', state: 'discovered', size: 32 },
  ...Array.from({ length: 30 }, (_, n) => ({ id: 10 + n, dir: ROOT, name: `z-synced-${n}.md`, state: 'synced', size: 20 })),
];
const state = {
  browseCalls: [],
  batches: [],
  batchPaths: [],
  // Any mutating request that is not the bounded batch loop (e.g. legacy /sync).
  otherMutations: [],
  failNextTransport: false,
  failBrowse: false,
  failEntry: 4,
  responseDelay: 0,
  allowStalledCursor: false,
};
const source = () => ({
  id: 1, name: '合成 WebDAV', base_url: 'https://dav.example/', root_path: ROOT, username: 'demo',
  has_password: true, is_enabled: true, recursive: true, trusted_private_network: false,
  include_extensions: ['.md'], ignore_patterns: [], remote_delete_policy: 'trash', sync_status: 'idle',
  last_error: null, last_scan_at: '2026-10-22T00:00:00Z', last_sync_at: null,
  entry_counts: { total: 7, pending: 4, synced: 1, failed: 1, missing: 0, suspected_missing: 0 },
  scan: null,
});

function listDirectory(directory) {
  const prefix = directory === ROOT ? `${ROOT}/` : `${directory}/`;
  const names = new Map();
  const items = [];
  for (const file of files.filter((item) => item.dir === directory)) {
    items.push({ kind: 'file', ...file, remote_path: `${prefix}${file.name}`, document_id: file.state === 'synced' ? 31 : null, ignore_reason: file.ignore_reason ?? null, resume_state: null, missing_count: 0, keep_snapshot: false });
  }
  for (const file of files) {
    if (!file.dir.startsWith(prefix) || file.dir === directory) continue;
    const name = file.dir.slice(prefix.length).split('/')[0];
    if (names.has(name)) continue;
    names.set(name, true);
    const subtree = files.filter((item) => item.dir === `${prefix}${name}` || item.dir.startsWith(`${prefix}${name}/`));
    items.push({
      kind: 'folder', name, remote_path: `${prefix}${name}`, file_count: subtree.length,
      pending_count: subtree.filter((item) => ['discovered', 'changed', 'failed'].includes(item.state)).length,
      failed_count: subtree.filter((item) => item.state === 'failed').length,
    });
  }
  const kindOrder = item => (item.kind === 'folder' ? 0 : 1);
  return items.sort((a, b) => kindOrder(a) - kindOrder(b) || a.remote_path.localeCompare(b.remote_path));
}
const fixture = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture.test');
  // Buffer the request body before routing so POST bodies are always available.
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    let raw = '';
    req.on('data', chunk => { raw += chunk; });
    req.on('end', () => route(req, res, url, raw));
    return;
  }
  route(req, res, url, '');
});

function route(req, res, url, raw) {
  const json = (body, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
  if (url.pathname === '/api/auth/status') return json({ authenticated: true, setup_required: false, admin: { id: 1, username: 'demo' } });
  if (url.pathname === '/api/workspaces/current') return json({ slug: 'default', name: '合成空间', status: 'active', settings: {} });
  if (url.pathname === '/api/workspaces') return json([{ slug: 'default', name: '合成空间', status: 'active', settings: {} }]);
  if (url.pathname === '/api/webdav') return json([source()]);
  // Guard the contract: imports only ever go through the bounded batch loop.
  if (req.method === 'POST' && !url.pathname.endsWith('/sync-batch')) {
    state.otherMutations.push(url.pathname);
  }
  if (url.pathname.endsWith('/browse')) {
    state.browseCalls.push(url.search);
    if (state.failBrowse) return json({ detail: '合成目录读取失败' }, 503);
    const directory = url.searchParams.get('directory') || ROOT;
    const q = url.searchParams.get('q') || '';
    const status = url.searchParams.get('state') || '';
    const recursive = url.searchParams.get('recursive') === 'true' || Boolean(q);
    const page = Number(url.searchParams.get('page') || 1);
    const pageSize = Number(url.searchParams.get('page_size') || 25);
    const inventory = recursive ? files.filter(file => file.dir === directory || file.dir.startsWith(`${directory}/`)).map(file => ({ kind: 'file', ...file, file_size: file.size, remote_path: `${file.dir}/${file.name}`, missing_count: 0 })) : listDirectory(directory);
    const all = inventory.filter((item) => {
      if (status && (item.kind !== 'file' || item.state !== status)) return false;
      if (q && !item.remote_path.includes(q)) return false;
      return true;
    });
    const flat = recursive ? all.filter((item) => item.kind === 'file') : all;
    const rows = flat.slice((page - 1) * pageSize, page * pageSize);
    const descendants = files.filter((file) => file.dir === directory || file.dir.startsWith(directory === ROOT ? `${ROOT}/` : `${directory}/`));
    return json({
      root: ROOT, directory, parent: directory === ROOT ? null : directory.slice(0, directory.lastIndexOf('/')),
      page, page_size: pageSize, total: flat.length, items: rows,
      pending_count: descendants.filter((file) => ['discovered', 'changed'].includes(file.state)).length,
      failed_count: descendants.filter((file) => file.state === 'failed').length,
    });
  }
  if (url.pathname.endsWith('/sync-batch') && req.method === 'POST') {
    const body = raw ? JSON.parse(raw) : {};
    state.batches.push(body);
    state.batchPaths.push(url.pathname);
    if (state.failNextTransport) { state.failNextTransport = false; res.destroy(); return; }
    const directory = body.directory ?? null;
    const pool = body.entry_ids !== undefined
      ? body.entry_ids
      : files.filter((file) => (directory === null || file.dir === directory || file.dir.startsWith(`${directory}/`))
        && (body.failed_only ? file.state === 'failed' : ['discovered', 'changed', 'failed'].includes(file.state)))
        .map((file) => file.id);
    const after = Number(body.after_id || 0);
    const next = pool.filter((id) => id > after)[0];
    const through = body.through_id !== undefined ? body.through_id : pool[pool.length - 1] ?? 0;
    if (next === undefined) return json({ ok: true, imported: 0, updated: 0, unchanged: 0, failed: 0, skipped: 0, processed: 0, after_id: after, through_id: through, has_more: false, errors: [] });
    const file = files.find((item) => item.id === next);
    const failed = file.id === state.failEntry;
    const result = {
      ok: !failed, imported: failed ? 0 : 1, updated: 0, unchanged: 0, failed: failed ? 1 : 0, skipped: 0,
      processed: 1, after_id: state.allowStalledCursor ? after : next, through_id: through,
      has_more: pool.some((id) => id > next),
      errors: failed ? [{ entry_id: file.id, message: '合成远端读取失败' }] : [],
    };
    if (state.responseDelay) return setTimeout(() => json(result), state.responseDelay);
    return json(result);
  }
  if (url.pathname.endsWith('/allow-reimport')) return json({ ok: true });
  if (url.pathname.endsWith('/keep-snapshot')) return json({ ok: true });
  return json({ detail: 'fixture route not needed' }, 404);
}

const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const apiPort = await listen(fixture);
  const reservation = http.createServer(); const port = await listen(reservation);
  await new Promise(resolve => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(process.execPath, ['server.cjs', '--dev', '-H', '127.0.0.1', '--port', String(port)], {
    cwd: path.join(__dirname, '../..'),
    env: { ...process.env, API_URL: `http://127.0.0.1:${apiPort}`, NEXT_TELEMETRY_DISABLED: '1', CANGZHI_WEB_BASE_PATH: '' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '', browser;
  server.stdout.on('data', data => { log += data; });
  server.stderr.on('data', data => { log += data; });
  try {
    for (let i = 0; i < 160; i++) {
      try { if ((await fetch(`${base}/api/auth/status`)).ok) break; } catch { /* startup */ }
      await delay(250);
    }
    browser = await chromium.launch({
      headless: true,
      executablePath: process.env.CANGZHI_TEST_CHROMIUM || ['/usr/bin/google-chrome', '/usr/bin/chromium'].find(file => fs.existsSync(file)),
    });
    const page = await browser.newPage(); const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await page.goto(`${base}/settings/sources`);

    const panel = page.getByRole('region', { name: '合成 WebDAV 文件目录' });
    const button = (name) => page.getByRole('button', { name, exact: true });

    await button('查看文件').click();
    await panel.getByText('root-a.md', { exact: true }).waitFor();
    assert.equal(await page.getByRole('dialog').count(), 0);

    // 1. The top sync button opens the browser and confirms the source-wide scope.
    await button('同步入库').click();
    await panel.waitFor();
    const dialog = page.getByRole('dialog');
    await dialog.getByText(/同步入库“合成 WebDAV”全部待处理文件/).waitFor();
    await dialog.getByText(/不受当前目录、搜索词或状态筛选影响/).waitFor();
    await dialog.getByText(/请保持本页打开/).waitFor();
    await dialog.getByRole('button', { name: '确认导入' }).click();
    // The top button must drive the same bounded batch loop, never legacy /sync.
    await panel.getByText(/已完成 \d+ 个文件/).waitFor();
    assert.equal(state.batchPaths.length >= 1, true);
    assert.deepEqual(state.batchPaths.every(p => p === '/api/webdav/1/sync-batch'), true);
    assert.deepEqual(state.otherMutations, []);
    const fullScope = state.batches[0];
    assert.equal(fullScope.directory, ROOT);
    assert.equal(fullScope.failed_only, undefined);
    assert.equal('entry_ids' in fullScope, false);
    assert.equal(fullScope.limit, 1);
    // Sequential one-file requests; the cursor advances and through_id stays frozen.
    assert.equal(state.batches.length >= 2, true);
    assert.equal(state.batches[0].after_id, 0);
    assert.equal(state.batches[0].through_id, undefined);
    assert.equal(state.batches[1].after_id, 1);
    assert.equal(state.batches[1].through_id, 7);
    assert.deepEqual(state.batches.map(body => body.limit), state.batches.map(() => 1));
    await panel.getByRole('button', { name: '收起结果' }).click();

    // 2. Folders and files share the server page; drilling uses the breadcrumb.
    assert.equal(state.browseCalls.length >= 1, true);
    await panel.getByRole('button', { name: /📁 team/ }).click();
    await panel.getByText('team-c.md', { exact: true }).waitFor();
    assert.ok(state.browseCalls.some(call => call.includes('directory=%2Fdocs%2Fteam')));
    await panel.getByRole('button', { name: '返回上级' }).click();
    await panel.getByText('root-a.md', { exact: true }).waitFor();

    // Real page transitions retain explicit selection in the same directory.
    await panel.getByRole('button', { name: '选中本页可导入文件', exact: true }).click();
    await panel.getByRole('button', { name: '下一页' }).click();
    await panel.getByText(/第 2 \/ 2 页/).waitFor();
    assert.equal(await panel.getByText('root-a.md', { exact: true }).count(), 0);
    assert.ok(state.browseCalls.some(call => call.includes('page=2')));
    assert.equal(await panel.getByRole('button', { name: '导入选中（1）' }).isEnabled(), true);
    await panel.getByRole('button', { name: '上一页' }).click();
    await panel.getByText('root-a.md', { exact: true }).waitFor();

    // 3. Select-page selection sends exactly the selected ids.
    state.batches.length = 0;
    await button('导入选中（1）').click();
    await dialog.getByRole('button', { name: '确认导入' }).click();
    await panel.getByText(/已完成 \d+ 个文件/).waitFor();
    const selection = state.batches[0];
    assert.deepEqual(selection.entry_ids, [1]);
    assert.equal(selection.limit, 1);
    assert.equal('directory' in selection, false);
    assert.equal(state.batches.length, 1);

    // 4. Folder drill-down + search widen only the listing, never the scope.
    await panel.getByRole('button', { name: '选中本页可导入文件', exact: true }).click();
    await panel.getByRole('button', { name: /📁 team/ }).click();
    await panel.getByText('team-c.md', { exact: true }).waitFor();
    assert.equal(await button('导入选中（0）').isDisabled(), true);
    state.batches.length = 0;
    await panel.getByRole('button', { name: '选中本页可导入文件', exact: true }).click();
    await button('导入选中（2）').click();
    await dialog.getByRole('button', { name: '确认导入' }).click();
    await panel.getByText(/已完成 \d+ 个文件/).waitFor();
    const inFolder = state.batches;
    assert.deepEqual([...new Set(inFolder.map(body => body.entry_ids.join(',')))], ['3,4']);
    assert.equal(inFolder.length, 2);
    await panel.locator('li').filter({ hasText: '合成远端读取失败' }).waitFor();

    // 5. Failed-only retry scope includes this directory's descendants.
    state.batches.length = 0;
    await panel.getByRole('button', { name: '重试本目录失败文件' }).click();
    await dialog.getByText(/子目录/).waitFor();
    await dialog.getByRole('button', { name: '确认导入' }).click();
    await panel.getByText(/已完成 1 个文件/).waitFor();
    assert.deepEqual(state.batches[0], { after_id: 0, limit: 1, directory: '/docs/team', failed_only: true });
    assert.ok(state.batches.every(body => body.entry_ids === undefined));

    // 7. A stalled cursor stops instead of looping forever.
    state.batches.length = 0;
    state.allowStalledCursor = true;
    await panel.getByRole('button', { name: '导入当前目录' }).click();
    await dialog.getByText(/不受上方搜索词与状态筛选影响/).waitFor();
    await dialog.getByRole('button', { name: '确认导入' }).click();
    await panel.getByText(/服务未推进导入位置/).waitFor();
    assert.equal(state.batches.length, 1);
    state.allowStalledCursor = false;

    // 8. A transport failure stops with retry guidance and keeps imported work.
    state.batches.length = 0;
    state.failNextTransport = true;
    await panel.getByRole('button', { name: '导入当前目录' }).click();
    await dialog.getByRole('button', { name: '确认导入' }).click();
    await panel.getByText(/已入库 \d+ 个文件保留，可重新选择剩余文件继续/).waitFor();
    assert.equal(state.batches.length, 1);
    await delay(600);
    assert.equal(state.batches.length, 1);

    // 9. The user can stop between files without a runaway loop.
    state.batches.length = 0;
    state.responseDelay = 2000;
    await panel.getByRole('button', { name: '导入当前目录' }).click();
    await dialog.getByRole('button', { name: '确认导入' }).click();
    await panel.getByRole('button', { name: '停止后续文件' }).waitFor();
    assert.equal(await button('编辑配置').isDisabled(), true);
    assert.equal(await button('扫描文件').isDisabled(), true);
    await panel.getByRole('button', { name: '停止后续文件' }).click();
    await panel.getByText(/已停止：完成/).waitFor();
    assert.equal(state.batches.length <= 2, true);
    await delay(2200);
    assert.equal(state.batches.length <= 2, true);

    // 10. Leaving or closing during an import is warned about.
    state.batches.length = 0;
    await panel.getByRole('button', { name: '导入当前目录' }).click();
    await dialog.getByRole('button', { name: '确认导入' }).click();
    await panel.getByRole('button', { name: '停止后续文件' }).waitFor();
    const dismissals = [];
    page.once('dialog', async nativeDialog => { dismissals.push(nativeDialog.message()); await nativeDialog.dismiss(); });
    await page.locator('a[href="/documents"]').first().click();
    await delay(300);
    assert.equal(dismissals.some(message => /正在逐个导入远端文件/.test(message)), true);
    assert.ok(page.url().endsWith('/settings/sources'));
    await panel.getByRole('button', { name: '停止后续文件' }).click();
    await panel.getByText(/已停止：完成/).waitFor();
    await delay(2200);
    state.responseDelay = 0;
    await panel.getByRole('button', { name: '返回上级' }).click();
    await panel.getByText('root-a.md', { exact: true }).waitFor();

    // 11. Selection is cleared on scope change but kept across page changes.
    await panel.getByRole('button', { name: '选中本页可导入文件', exact: true }).click();
    const selectedBefore = await panel.getByRole('button', { name: /导入选中（\d+）/ }).textContent();
    assert.match(selectedBefore, /导入选中（1）/);
    await panel.getByRole('button', { name: /📁 other/ }).click();
    await panel.getByText('other-f.md', { exact: true }).waitFor();
    assert.equal(await panel.getByRole('button', { name: '清空选择' }).isDisabled(), true);
    assert.match(await panel.getByRole('button', { name: /导入选中（\d+）/ }).textContent(), /导入选中（0）/);

    // 12. A failed directory load disables actions until the retry succeeds.
    state.failBrowse = true;
    await panel.getByRole('button', { name: '刷新目录' }).click();
    await panel.getByText('合成目录读取失败').waitFor();
    assert.equal(await panel.getByRole('button', { name: '导入当前目录' }).isDisabled(), true);
    assert.equal(await panel.getByRole('button', { name: '返回上级' }).isDisabled(), true);
    assert.equal(await panel.getByRole('checkbox', { name: '选中本页可导入文件' }).isDisabled(), true);
    state.failBrowse = false;
    await panel.getByRole('button', { name: '重试读取' }).click();
    await panel.getByText('合成目录读取失败').waitFor({ state: 'hidden' });
    await panel.getByRole('button', { name: '正在刷新…' }).waitFor({ state: 'hidden' });
    await panel.getByText('other-f.md', { exact: true }).waitFor();
    assert.equal(await panel.getByRole('button', { name: '导入当前目录' }).isDisabled(), false);

    // Search spans descendants but retains the current explicit root scope.
    await panel.getByRole('button', { name: '根目录 /docs', exact: true }).click();
    await panel.getByText('root-a.md', { exact: true }).waitFor();
    await panel.getByLabel('搜索文件名', { exact: true }).fill('deep-e');
    await panel.getByRole('button', { name: '搜索', exact: true }).click();
    await panel.getByText('deep-e.md', { exact: true }).waitFor();
    assert.ok(state.browseCalls.some(call => call.includes('q=deep-e') && call.includes('recursive=true')));
    await panel.getByRole('button', { name: '清除搜索' }).click();
    await panel.getByText('root-a.md', { exact: true }).waitFor();

    // 13. Bounded list and mobile/desktop widths.
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      const section = panel;
      assert.equal(await section.evaluate(el => el.scrollWidth <= el.clientWidth + 1), true);
      assert.equal(await section.locator('table').evaluate(el => el.parentElement.clientHeight <= 384), true);
      await section.getByRole('button', { name: '刷新目录' }).scrollIntoViewIfNeeded();
    }
    await page.setViewportSize({ width: 390, height: 900 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true);

    assert.deepEqual(errors, []);
    process.stdout.write('PASS paged browse, folder drill-down, exact selection payloads, partial-failure progress, stall and transport stops without runaway, leave warning, stale-scope lock and 390/1440 layout; synthetic API only\n');
  } catch (error) {
    process.stderr.write(log.slice(-8000));
    const activePage = browser?.contexts()[0]?.pages()[0];
    if (activePage) process.stderr.write((await activePage.locator('body').innerText()).slice(-7000));
    throw error;
  }
  finally { await browser?.close(); server.kill('SIGTERM'); await new Promise(resolve => fixture.close(resolve)); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
