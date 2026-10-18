/* eslint-disable @typescript-eslint/no-require-imports -- Isolated browser regression with synthetic fixtures. */
// No deployed API, credentials, real documents, or model requests are used.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const { spawn } = require('node:child_process');
const path = require('node:path');
const { chromium } = require('playwright');
const counts = { preview: 0, chunks: 0, candidates: 0 };
let revision = 100, blob = 50;
let apiRevision = 'a'.repeat(40);
const doc = () => ({ id: 21, title: '合成视图测试', source_type: 'file', content_kind: 'document',
  tags: [], categories: [], primary_category: null, summary: null, origin: null, created_at: '2026-10-18',
  current_version: { id: 31, version_number: 1, processing_status: 'ready', raw_content: '合成解析正文',
    structured_content: {}, meta: {}, blob: { id: blob, original_filename: 'fixture.pdf', content_type: 'application/pdf' }, preview_blob: null } });
const fixture = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture.test');
  const route = url.pathname;
  const json = (body, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
  if (route === '/api/auth/status') return json({ authenticated: true, setup_required: false, admin: { id: 1, username: 'demo' } });
  if (route === '/api/system/upload-settings') return json({ max_upload_size_mb: 50 });
  if (route === '/api/system/version') return json({ version: 'v1.3.2', revision: apiRevision, built_at: '2026-10-18T00:00:00Z', dirty: false });
  if (route === '/api/workspaces/current') return json({ slug: 'default', name: '合成空间', status: 'active', settings: {} });
  if (route === '/api/workspaces') return json([{ slug: 'default', name: '合成空间', status: 'active', settings: {} }]);
  if (['/api/categories', '/api/tags', '/api/datasets'].includes(route)) return json([]);
  if (route === '/api/documents/21') return json(doc());
  if (route.endsWith('/latest-job')) return json(null);
  if (route.endsWith('/processing-status')) return json({ overall_status: 'processing', has_active_jobs: true,
    chunk_revision: revision, keyword_searchable: true, vector_searchable: false,
    stages: { parsing: { status: 'completed' }, understanding: { status: 'completed' },
      chunking: { status: 'completed', child_chunks: 10 }, embedding: { status: 'processing', total: 10, completed: 5, failed: 0 } } });
  if (route.endsWith('/original')) {
    counts.preview++;
    // HTML stands in for the embedded viewer: verifies retained DOM/network and
    // embedded state, not Chrome's implementation of PDF zoom/page restoration.
    res.writeHead(200, { 'content-type': 'text/html', 'cache-control': 'no-store' });
    return res.end('<!doctype html><title>合成预览</title><input aria-label="预览内部状态" value="initial">');
  }
  if (route.endsWith('/chunks-preview')) {
    counts.chunks++;
    const offset = Number(url.searchParams.get('offset') || 0);
    return json({ child_total: 10, parent_total: 1, offset, limit: 5, quality: null,
      items: Array.from({ length: 5 }, (_, i) => ({ id: revision + offset + i, order_index: offset + i,
        heading_path: ['合成章节'], page: 1, char_count: 12, content: `在线内容 ${offset + i + 1}`, truncated: false })) });
  }
  if (route.endsWith('/chunking-preview')) {
    counts.candidates++;
    return json({ calls: 0, assisted_blocks: 0, total_blocks: 10, accepted_windows: 0, failed_windows: 0,
      provider_available: false, skipped_dataset: false, baseline: { children: 10, short_children: 1 },
      candidate: { children: 8, short_children: 0 }, samples: [{ content: '合成候选结果', page: 1, mode: 'rule' }] });
  }
  return json({ detail: 'fixture route not needed' }, 404);
});
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, message) {
  for (let i = 0; i < 120; i++) { if (await check()) return; await delay(250); }
  throw new Error(message);
}
async function main() {
  const apiPort = await listen(fixture);
  const reservation = http.createServer(); const port = await listen(reservation);
  await new Promise(resolve => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(process.execPath, [require.resolve('next/dist/bin/next'), 'dev', '--hostname', '127.0.0.1', '--port', String(port)], {
    cwd: path.resolve(__dirname, '../..'),
    env: { ...process.env, API_URL: `http://127.0.0.1:${apiPort}`, NEXT_TELEMETRY_DISABLED: '1', CANGZHI_WEB_BASE_PATH: '',
      CANGZHI_VERSION: 'v1.3.2', CANGZHI_REVISION: 'a'.repeat(40), CANGZHI_BUILD_TIME: '2026-10-18T00:00:00Z', CANGZHI_BUILD_DIRTY: 'false' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '', browser;
  server.stdout.on('data', value => { log = (log + value).slice(-5000); });
  server.stderr.on('data', value => { log = (log + value).slice(-5000); });
  try {
    await until(async () => { try { return (await fetch(`${base}/api/auth/status`)).ok; } catch { return false; } }, 'Next server did not start');
    browser = await chromium.launch({ headless: true,
      executablePath: process.env.CANGZHI_TEST_CHROMIUM || ['/usr/bin/google-chrome', '/usr/bin/chromium'].find(file => fs.existsSync(file)) });
    const page = await browser.newPage(); const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await page.goto(`${base}/documents/21`);
    const tab = label => page.getByRole('tab', { name: label, exact: true });
    const panel = label => page.getByRole('tabpanel', { name: label, exact: true });
    await tab('原文版').waitFor();
    // The tablet breakpoint used to force a narrow scrolling strip beside the
    // description, hiding the last tab. Every tab must fit without scrolling.
    for (const width of [320, 390, 400, 688, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 800 });
      const layout = await page.getByRole('tablist', { name: '内容预览方式' }).evaluate(list => {
        const bounds = list.getBoundingClientRect();
        const title = list.previousElementSibling.getBoundingClientRect();
        return { overflow: list.scrollWidth > list.clientWidth + 1,
          overlap: bounds.left < title.right && bounds.right > title.left && bounds.top < title.bottom && bounds.bottom > title.top,
          tabs: Array.from(list.querySelectorAll('[role="tab"]')).map(button => {
            const rect = button.getBoundingClientRect();
            return { inside: rect.left >= bounds.left && rect.right <= bounds.right + 1,
              fits: button.scrollWidth <= button.clientWidth + 1, touch: rect.height >= 36 };
          }) };
      });
      assert.equal(layout.overflow, false, `tab strip overflow at ${width}px`);
      assert.equal(layout.overlap, false, `description overlaps tabs at ${width}px`);
      assert.equal(layout.tabs.length, 4);
      assert.ok(layout.tabs.every(button => button.inside && button.fits && button.touch), `clipped tab at ${width}px`);
    }
    const iframe = page.locator('iframe[title="合成视图测试 原文预览"]');
    await page.frameLocator('iframe[title="合成视图测试 原文预览"]').getByLabel('预览内部状态').fill('retained');
    const node = await iframe.elementHandle();
    assert.equal(counts.chunks, 0); assert.equal(counts.candidates, 0);
    await tab('解析版').click(); await panel('解析版').getByText('合成解析正文', { exact: true }).waitFor();
    await tab('原文版').click();
    assert.equal(await iframe.evaluate((current, previous) => current === previous, node), true);
    assert.equal(await page.frameLocator('iframe[title="合成视图测试 原文预览"]').getByLabel('预览内部状态').inputValue(), 'retained');
    assert.equal(counts.preview, 1);
    await tab('在线切片').click(); await panel('在线切片').getByRole('button', { name: '下一页' }).click();
    await panel('在线切片').getByText('显示 6–10 / 10', { exact: true }).waitFor();
    await tab('候选对比').click(); assert.equal(counts.candidates, 0);
    await panel('候选对比').getByRole('button', { name: '生成并对比候选', exact: true }).click();
    await panel('候选对比').getByText('合成候选结果', { exact: true }).waitFor();
    const chunkCount = counts.chunks;
    await tab('在线切片').click(); await panel('在线切片').getByText('显示 6–10 / 10', { exact: true }).waitFor();
    await tab('候选对比').click(); await panel('候选对比').getByText('合成候选结果', { exact: true }).waitFor();
    assert.equal(counts.chunks, chunkCount); assert.equal(counts.candidates, 1); assert.equal(counts.preview, 1);
    revision = 200;
    await panel('候选对比').getByRole('button', { name: '忽略缓存并重新生成' }).waitFor({ state: 'detached' });
    assert.equal(counts.candidates, 1); // never auto-call AI on revision changes
    await tab('在线切片').click(); await panel('在线切片').getByText('显示 1–5 / 10', { exact: true }).waitFor();
    assert.equal(counts.preview, 1);
    blob = 51;
    await until(async () => await iframe.count() === 0, 'New preview blob did not invalidate retained iframe');
    // Inactive invalidated panels are lazy, not downloading in the background.
    assert.equal(await iframe.count(), 0);
    await tab('原文版').click();
    await until(() => counts.preview === 2, 'Updated preview was not loaded');
    assert.equal(await page.frameLocator('iframe[title="合成视图测试 原文预览"]').getByLabel('预览内部状态').inputValue(), 'initial');
    assert.deepEqual(errors, []);
    await page.goto(`${base}/settings/uploads`);
    const version = page.locator('#system-version');
    await version.locator('summary').click();
    await version.getByText('Web/API版本一致', { exact: true }).waitFor();
    apiRevision = 'b'.repeat(40);
    await version.getByRole('button', { name: '重新核对' }).click();
    await version.getByText('Web/API版本不一致，请确认各服务已完成更新并刷新页面。', { exact: true }).waitFor();
    await page.setViewportSize({ width: 320, height: 800 });
    assert.equal(await version.evaluate(element => element.scrollWidth <= element.clientWidth + 1), true);
    assert.deepEqual(errors, []);
    process.stdout.write('PASS version display, Web/API match and mismatch, narrow-screen metadata wrapping\n');
    process.stdout.write('PASS responsive tabs 320–1440px, retained iframe, lazy panels, chunk pagination, candidate result, revision invalidation; no real model calls\n');
  } catch (error) { process.stderr.write(log); throw error; }
  finally { await browser?.close(); server.kill('SIGTERM'); await new Promise(resolve => fixture.close(resolve)); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
