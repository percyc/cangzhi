/* eslint-disable @typescript-eslint/no-require-imports -- Isolated browser acceptance with synthetic data. */
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const workspace = { id: 1, slug: 'default', name: '布局验收', status: 'active', settings: {} };
let tokens = Array.from({ length: 11 }, (_, i) => ({ id: i + 1, name: `验收令牌 ${i + 1}`, token_prefix: `test-${i}`, scopes: ['knowledge:read'],
  expires_at: i === 0 ? '2000-01-01T00:00:00Z' : null, revoked_at: i === 10 ? '2020-01-01T00:00:00Z' : null,
  last_used_at: null, created_at: '2020-01-01T00:00:00Z', workspace_id: 1 }));
const stage = { status: 'completed', message: '已完成' };
const pipeline = { overall_status: 'completed', keyword_searchable: true, vector_searchable: false, stages: {
  parsing: stage, understanding: stage, chunking: { ...stage, child_chunks: 5 },
  embedding: { status: 'disabled', message: '未启用', total: 0, completed: 0, missing: 0, failed: 0 },
} };
const docs = Array.from({ length: 100 }, (_, i) => ({ id: i + 1, title: `合成资料 ${i + 1}：用于验证长列表和翻页布局`,
  source_type: 'file', content_kind: 'document', tags: [], categories: [], origin: null, primary_category: null,
  current_version: { id: i + 1, processing_status: 'ready' }, summary: { summary: '这是合成测试内容。'.repeat(14) },
  updated_at: '2020-01-01T00:00:00Z', created_at: '2020-01-01T00:00:00Z', pipeline }));
const fixture = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  const json = (value, code = 200) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
  if (url.pathname === '/api/auth/status') return json({ authenticated: true, setup_required: false, admin: { username: 'test' } });
  if (url.pathname === '/api/workspaces/current') return json(workspace);
  if (url.pathname === '/api/workspaces') return json([workspace]);
  if (url.pathname === '/api/system/version') return json({ version: 'test', revision: 'test', dirty: false });
  if (url.pathname === '/api/access-tokens') return json({ items: tokens, available_scopes: [] });
  if (req.method === 'DELETE' && url.pathname === '/api/access-tokens/11') { tokens = tokens.filter(t => t.id !== 11); return json({}); }
  if (url.pathname === '/api/categories' || url.pathname === '/api/tags') return json([]);
  if (url.pathname === '/api/documents/overview') {
    res.setHeader('X-Total-Count', String(docs.length));
    const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 24);
    return json(docs.slice(offset, offset + limit));
  }
  if (url.pathname.includes('inbox')) {
    const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 25);
    return json({ items: docs.slice(offset, offset + limit), total: docs.length, has_processing: false,
      counts: { all: 100, attention: 100, processing: 0, failed: 0, completed: 100, source_issue: 0, not_vectorized: 0, needs_organization: 0 } });
  }
  return json({ detail: 'Unmocked route: ' + url.pathname }, 404);
});

async function main() {
  const apiPort = await listen(fixture), reservation = http.createServer(), port = await listen(reservation);
  await new Promise(resolve => reservation.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.execPath, [require.resolve('next/dist/bin/next'), 'dev', '--hostname', '127.0.0.1', '--port', String(port)], {
    cwd: path.resolve(__dirname, '../..'), env: { ...process.env, API_URL: `http://127.0.0.1:${apiPort}`, NEXT_TELEMETRY_DISABLED: '1', CANGZHI_WEB_BASE_PATH: '' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '', browser;
  server.stdout.on('data', data => { log = (log + data).slice(-12000); });
  server.stderr.on('data', data => { log = (log + data).slice(-12000); });
  const exited = new Promise(resolve => server.once('exit', resolve));
  const screenshots = fs.mkdtempSync(path.join(os.tmpdir(), 'cz-list-layout-'));
  try {
    let ready = false;
    for (let i = 0; i < 160; i++) {
      try { ready = (await fetch(`${origin}/api/auth/status`)).ok; } catch {}
      if (ready) break;
      await delay(250);
    }
    assert.ok(ready, 'Next server must start');
    browser = await chromium.launch({ headless: true, executablePath: ['/usr/bin/google-chrome', '/usr/bin/chromium'].find(fs.existsSync) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    for (const route of ['/documents', '/inbox']) {
      await page.goto(origin + route);
      await page.getByRole('navigation', { name: '列表分页' }).getByText('下一页').waitFor();
      await page.getByRole('button', { name: '下一页', exact: true }).click();
      await page.waitForFunction(() => document.querySelector('input[name="page"]')?.value === '2');
      for (const [width, height] of [[1440,900], [1280,720], [1024,768], [1440,560], [390,844], [320,640]]) {
        await page.setViewportSize({ width, height });
        await delay(150);
        const metrics = await page.evaluate(() => {
          const viewport = document.querySelector('.list-viewport'), footer = document.querySelector('.list-pagination');
          return { overflow: document.documentElement.scrollWidth > innerWidth + 1, bodyHeight: document.documentElement.scrollHeight,
            footerBottom: footer.getBoundingClientRect().bottom, listHeight: viewport.clientHeight,
            scrollable: viewport.scrollHeight > viewport.clientHeight };
        });
        if (metrics.overflow) console.log('overflow elements', await page.evaluate(() => [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 1).slice(0, 10).map(e => ({tag:e.tagName, className:e.className, right:e.getBoundingClientRect().right}))));
        assert.equal(metrics.overflow, false, `${route} ${width} horizontal overflow`);
        if (width >= 1024 && height >= 700) {
          assert.ok(metrics.footerBottom <= height + 1, `${route} footer visible`);
          assert.ok(metrics.bodyHeight <= height + 2, `${route} no outer scroll`);
          assert.ok(metrics.listHeight >= 150 && metrics.scrollable, `${route} useful scroll region`);
        }
      }
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.getByText('如何使用此页', { exact: true }).click();
      await delay(200);
      assert.ok(await page.locator('.list-pagination').evaluate(e => e.getBoundingClientRect().bottom <= innerHeight + 1), 'expanded help reserves height');
      await page.getByText('如何使用此页', { exact: true }).click();
      await page.screenshot({ path: path.join(screenshots, route.slice(1) + '.png') });
      // Jumping to the same page must not leave the screen permanently loading.
      await page.getByRole('button', { name: '跳转', exact: true }).click();
      assert.equal(await page.getByRole('button', { name: '下一页', exact: true }).isEnabled(), true);
    }
    await page.goto(origin + '/documents');
    await page.getByLabel('每页条数').selectOption('12');
    await page.getByRole('status').filter({ hasText: '第 1–12 条 / 共 100 条' }).waitFor();
    await page.waitForFunction(() => !document.querySelector('select[aria-label="每页条数"]').disabled);
    assert.equal(await page.locator('.list-viewport h2').count(), 12);
    await page.goto(origin + '/settings/access');
    const card = page.locator('.token-list-card');
    await card.getByText('9 个有效').waitFor();
    assert.equal(await card.locator('li').count(), 5);
    await card.getByLabel('跳转页码').fill('3');
    await card.getByRole('button', { name: '跳转', exact: true }).click();
    await card.getByRole('button', { name: '删除', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.token-list-card input[name="page"]')?.value === '2');
    assert.equal(await card.locator('li').count(), 5, 'delete last page clamps to prior page');
    await card.getByLabel('令牌状态').selectOption('expired');
    assert.equal(await card.locator('li').count(), 1);
    await card.getByLabel('搜索令牌名称或前缀').fill('不存在');
    await card.getByText('没有符合筛选条件的令牌，请调整名称或状态。').waitFor();
    await card.getByLabel('搜索令牌名称或前缀').fill('');
    await card.getByLabel('令牌状态').selectOption('all');
    for (const [width, height] of [[1440,900], [1280,720], [390,844], [320,640]]) {
      await page.setViewportSize({ width, height }); await delay(100);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      if (width >= 1024) {
        const box = await card.boundingBox();
        assert.ok(box.height <= Math.max(380, height - 250) + 1, 'token panel is bounded');
      }
    }
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.evaluate(() => scrollTo(0, 0));
    await delay(200);
    assert.ok(await card.locator('.list-pagination').evaluate(e => e.getBoundingClientRect().bottom <= innerHeight + 1), 'token pagination visible without page scrolling');
    await page.screenshot({ path: path.join(screenshots, 'tokens.png') });
    assert.deepEqual(errors, []);
    console.log('PASS list workspace: pagination, delete clamp, filters, responsive heights; screenshots:', screenshots);
  } catch (error) { process.stderr.write(log); throw error; }
  finally { await browser?.close(); server.kill('SIGTERM'); await exited; await new Promise(resolve => fixture.close(resolve)); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
