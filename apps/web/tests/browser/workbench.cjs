/* eslint-disable @typescript-eslint/no-require-imports -- Isolated Chromium acceptance runner. */
// Synthetic fixtures only. Starts an isolated loopback API and Next dev server;
// never connects to a deployed service, reads credentials or calls a model.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');
const webRoot = path.resolve(__dirname, '../..');
const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), 'cangzhi-workbench-browser-'));
let authenticated = true;
let askRequests = 0;
let noteSaves = 0;
let releaseSettingsSave;
const modelConfig = { provider: 'openai', openai_base_url: 'https://model.invalid/v1', openai_model: 'fixture', has_api_key: true, ollama_base_url: '', ollama_model: '', timeout_seconds: 30, embedding_provider: 'disabled', embedding_base_url: '', embedding_model: '', has_embedding_api_key: false, embedding_timeout_seconds: 30, ocr_provider: 'disabled', ocr_base_url: '', ocr_model: '', has_ocr_api_key: false, ocr_timeout_seconds: 30, ocr_confidence_threshold: 600, ocr_min_chars: 8, ocr_max_external_pages: 20 };
const note = { id: 21, title: '演示笔记', source_type: 'note', source_url: null, content_kind: 'note', created_at: '2026-10-04', updated_at: '2026-10-04', categories: [], tags: [], primary_category: null, summary: null, origin: null,
  current_version: { id: 31, version_number: 1, processing_status: 'ready', raw_content: '这是一段合成测试正文，不包含真实资料。', structured_content: {}, meta: {} } };
const workspaces = [
  { slug: 'default', name: '演示空间', status: 'active', is_default: true, settings: {} },
  { slug: 'demo-two', name: '第二空间', status: 'active', is_default: false, settings: {} },
];
const citation = { id: 1, document_id: 21, document_version_id: 31, chunk_id: 41, title: '演示笔记', heading_path: ['示例章节'], page: null, paragraph_index: 0, source_type: 'note', source_url: null, snippet: '这是供界面验收使用的原始证据。' };
const filters = { categories: [], tags: [], source_types: [{ value: 'note', label: '随手记', document_count: 1 }], connectors: [] };
const requests = [];
const fixture = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture.test');
  const route = url.pathname;
  requests.push(`${req.method} ${route}`);
  let raw = ''; for await (const part of req) raw += part;
  const body = raw ? JSON.parse(raw) : {};
  const json = (value, code = 200, headers = {}) => { res.writeHead(code, { 'content-type': 'application/json', ...headers }); res.end(JSON.stringify(value)); };
  if (route === '/api/auth/status') return json({ authenticated, setup_required: false, admin: authenticated ? { id: 1, username: 'demo' } : null });
  if (route === '/api/auth/login') { authenticated = true; return json({ ok: true }); }
  if (!authenticated) return json({ detail: { message: '登录已过期，请重新登录' } }, 401);
  if (route === '/api/workspaces') return json(workspaces);
  if (route === '/api/workspaces/current') return json(workspaces[(req.headers.cookie || '').includes('demo-two') ? 1 : 0]);
  if (route === '/api/settings/ai') {
    if (req.method === 'PATCH') await new Promise(resolve => { releaseSettingsSave = resolve; });
    return json({ config: modelConfig });
  }
  if (route === '/api/embeddings/status') return json({ active_profile: { id: null, status: null, model: null, dim: null, provider: null }, profiles: [] });
  if (route === '/api/categories' || route === '/api/tags' || route === '/api/documents/overview' || route === '/api/datasets') return json([], 200, { 'X-Total-Count': '0' });
  if (route === '/api/notes' && req.method === 'POST') { noteSaves++; return json({ id: 21 }); }
  if (route === '/api/documents/21') return json(note);
  if (route === '/api/documents/21/latest-job') return json(null);
  if (route === '/api/documents/21/processing-status') return json({ overall_status: 'completed', keyword_searchable: true, vector_searchable: false, stages: { parsing: { status: 'completed' }, understanding: { status: 'completed' }, chunking: { status: 'completed', child_chunks: 1 }, embedding: { status: 'disabled', total: 0, completed: 0, failed: 0 } } });
  if (route === '/api/search/filters' || route === '/api/v1/knowledge/facets') return json(filters);
  if (route === '/api/search') return json({ query: body.query, backend: 'fts', total: 1, limit: 20, offset: 0, hits: [{ ...citation, score: 1, highlights: [], categories: [], tags: [], chunk: { id: 41, type: 'child', heading_path: ['示例章节'], page: null, paragraph_index: 0 } }], retrieval: { mode: 'keyword', vector_used: false, degraded_reason: null } });
  if (route === '/api/v1/knowledge/evidence/by-chunk/41') return json({ ...citation, evidence_type: 'text', document_type: 'note', context_markdown: '## 示例章节\n\n这是供界面验收使用的原始证据。', preview_url: null, original_url: null });
  if (route === '/api/v1/knowledge/scopes') return json({ items: [{ slug: 'all', name: '全部知识' }] });
  if (route === '/api/ask/status') return json({ provider_configured: true });
  if (route === '/api/datasets/summary') return json({ dataset_count: 0, document_count: 0, ready_count: 0, examples: [] });
  if (route === '/api/ask/conversations') return json(req.method === 'POST' ? { id: 1, title: '合成测试问答', turn_count: 0 } : { items: [] });
  if (route === '/api/v1/knowledge/ask') {
    askRequests++;
    assert.deepEqual(body.document_ids, [21]);
    return json({ question: body.question, answer: '这是有据可查的演示回答。[1]', insufficient_evidence: false, citations: [citation], evidence: [citation], provider: 'fixture', model: 'none', history: { conversation_id: 1, turn_id: 1 } });
  }
  return json({ detail: `Unmocked route: ${route}` }, 404);
});

const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(test, message) {
  for (let i = 0; i < 120; i++) { if (await test()) return; await delay(250); }
  throw new Error(message);
}

async function main() {
  const apiPort = await listen(fixture);
  const reservation = http.createServer(); const port = await listen(reservation); await new Promise(resolve => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  let serverLog = '';
  const server = spawn(process.execPath, [require.resolve('next/dist/bin/next'), 'dev', '--hostname', '127.0.0.1', '--port', String(port)], {
    cwd: webRoot, env: { ...process.env, API_URL: `http://127.0.0.1:${apiPort}`, NEXT_TELEMETRY_DISABLED: '1', CANGZHI_WEB_BASE_PATH: '' }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  server.stdout.on('data', value => { serverLog = (serverLog + value).slice(-6000); });
  server.stderr.on('data', value => { serverLog = (serverLog + value).slice(-6000); });
  let browser;
  try {
    await until(async () => { try { return (await fetch(`${base}/api/auth/status`)).ok; } catch { return false; } }, 'Isolated Next server failed to start');
    const executablePath = process.env.CANGZHI_TEST_CHROMIUM || ['/usr/bin/google-chrome', '/usr/bin/chromium'].find(file => fs.existsSync(file));
    browser = await chromium.launch({ headless: true, executablePath });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    // Browser is disposable and deliberately never uses an existing user profile.
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      return url.origin === base ? route.continue() : route.abort();
    });

    if (process.env.CANGZHI_TEST_ONLY !== 'navigation') {
    await page.goto(`${base}/notes/new`);
    await page.getByLabel('内容', { exact: false }).fill('演示空间的待保存草稿');
    await page.getByLabel('标题', { exact: false }).fill('草稿演示');
    await page.getByRole('link', { name: '搜资料', exact: true }).first().click();
    await page.waitForURL('**/search');
    await page.goto(`${base}/notes/new`);
    await until(async () => (await page.locator('#content').inputValue()) === '演示空间的待保存草稿', 'Draft was not restored after navigation');
    await context.addCookies([{ name: 'cangzhi_workspace', value: 'demo-two', url: base }]);
    await page.reload();
    await page.locator('#content').waitFor();
    assert.equal(await page.locator('#content').inputValue(), '');
    await context.addCookies([{ name: 'cangzhi_workspace', value: 'default', url: base }]);
    await page.reload();
    await until(async () => (await page.locator('#content').inputValue()) === '演示空间的待保存草稿', 'Workspace draft crossed or disappeared');

    authenticated = false;
    await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByRole('link', { name: /重新登录/ }).last().click();
    await page.waitForURL('**/login?**');
    await page.getByLabel('用户名', { exact: true }).fill('demo');
    await page.getByLabel('密码', { exact: true }).fill('synthetic-not-a-credential');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.waitForURL('**/notes/new');
    await until(async () => (await page.locator('#content').inputValue()) === '演示空间的待保存草稿', 'Draft missing after login return');
    assert.equal(noteSaves, 0);
    console.log('PASS draft navigation, workspace isolation, expired-session recovery');

    await page.goto(`${base}/search?q=演示`);
    await page.getByRole('button', { name: '演示笔记', exact: true }).focus();
    await page.keyboard.press('Enter');
    await page.getByRole('heading', { name: '演示笔记', exact: true }).waitFor();
    await page.getByRole('region', { name: '资料阅读区' }).getByRole('link', { name: '仅问这份资料', exact: true }).click();
    await page.waitForURL('**/ask?document_ids=21');
    assert.equal(askRequests, 0);
    await page.locator('textarea').fill('这份演示资料说了什么？');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.getByText('这是有据可查的演示回答。', { exact: false }).waitFor();
    await page.getByText('查看 1 条引用', { exact: true }).click();
    await page.getByRole('button', { name: /演示笔记/ }).last().click();
    await page.getByRole('heading', { name: '演示笔记', exact: true }).waitFor();
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.equal(askRequests, 1);
    await page.screenshot({ path: path.join(artifacts, 'desktop-workbench.png'), fullPage: true });
    console.log('PASS desktop list → preview → scoped question → inline evidence');

    await page.setViewportSize({ width: 390, height: 740 });
    await page.screenshot({ path: path.join(artifacts, 'mobile-workbench.png'), fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'Mobile has horizontal page overflow');
    await page.setViewportSize({ width: 390, height: 450 });
    await page.getByRole('button', { name: '问答', exact: true }).click();
    await page.locator('textarea').fill('模拟软键盘下的新问题');
    const inputBox = await page.locator('textarea').boundingBox();
    assert.ok(inputBox && inputBox.width > 100 && inputBox.height > 20);
    assert.ok(inputBox.y + inputBox.height <= 451, 'Composer falls below the keyboard-height viewport');
    await page.reload();
    await until(async () => (await page.locator('textarea').inputValue()) === '模拟软键盘下的新问题', 'Scoped ask draft disappeared after reload');
    console.log('PASS mobile region switch, narrow viewport and keyboard-height viewport');
    }

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`${base}/search`);
    await page.goto(`${base}/links/new`);
    await page.getByLabel('链接地址').fill('https://example.test/synthetic');
    page.once('dialog', dialog => dialog.dismiss());
    await page.getByRole('link', { name: '搜资料', exact: true }).first().click();
    assert.ok(page.url().endsWith('/links/new'));
    // Create an SPA history entry so back/forward exercises Next, not a reload.
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('link', { name: '搜资料', exact: true }).first().click();
    await page.waitForURL('**/search');
    await page.getByRole('heading', { name: '搜索资料', exact: true }).waitFor();
    await page.goBack();
    await page.waitForURL('**/links/new');
    await page.getByLabel('链接地址').fill('https://example.test/synthetic-again');
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    page.once('dialog', dialog => dialog.dismiss());
    await page.evaluate(() => window.history.forward());
    await delay(500);
    assert.ok(page.url().endsWith('/links/new'));
    assert.equal(await page.getByLabel('链接地址').inputValue(), 'https://example.test/synthetic-again');
    console.log('PASS unsaved link cancel, accept and browser-forward restoration');

    // Native anchor entries must not become a hole in navigation protection.
    await page.getByLabel('链接地址').fill('');
    await page.getByRole('link', { name: '搜资料', exact: true }).first().click();
    await page.waitForURL('**/search');
    await page.evaluate(() => { window.location.hash = 'fixture-anchor'; });
    await page.waitForURL('**/search#fixture-anchor');
    await page.evaluate(() => {
      const link = document.createElement('a'); link.href = '/links/new'; link.textContent = 'Browser fixture editor link'; document.body.append(link);
    });
    await page.getByRole('link', { name: 'Browser fixture editor link' }).click();
    await page.waitForURL('**/links/new');
    await page.getByLabel('链接地址').fill('https://example.test/hash-return');
    page.once('dialog', dialog => dialog.dismiss());
    await page.evaluate(() => history.back());
    await delay(500);
    assert.ok(page.url().endsWith('/links/new'));
    assert.equal(await page.getByLabel('链接地址').inputValue(), 'https://example.test/hash-return');
    page.once('dialog', dialog => dialog.accept());
    await page.evaluate(() => history.back());
    await page.waitForURL('**/search#fixture-anchor');
    await page.getByLabel('搜索关键词').waitFor();
    console.log('PASS native-anchor back navigation preserves unsaved editor');

    await page.goto(`${base}/settings?section=chat`);
    await page.locator('#api-key').fill('synthetic-fixture-value');
    let confirmations = 0;
    const rejectLeave = async dialog => { confirmations++; await dialog.dismiss(); };
    page.on('dialog', rejectLeave);
    await page.locator('.settings-rail').getByRole('link', { name: /知识源/ }).click();
    assert.equal(confirmations, 1);
    assert.ok(page.url().includes('/settings?section=chat'));
    page.off('dialog', rejectLeave);
    await page.getByRole('button', { name: '保存对话模型', exact: true }).click();
    await until(async () => Boolean(releaseSettingsSave), 'Settings save did not reach synthetic API');
    assert.equal(await page.locator('#api-key').isDisabled(), true);
    assert.equal(await page.locator('#openai-model').isDisabled(), true);
    releaseSettingsSave();
    await until(async () => !(await page.locator('#api-key').isDisabled()), 'Settings save did not unlock');
    assert.equal(await page.locator('#api-key').inputValue(), '');
    console.log('PASS settings leave confirmation and in-flight credential editor lock');
    assert.deepEqual(errors, []);
    console.log(`Browser acceptance passed. Synthetic screenshots: ${artifacts}`);
  } catch (error) {
    console.error(serverLog);
    throw error;
  } finally {
    releaseSettingsSave?.();
    await browser?.close();
    server.kill('SIGTERM');
    await new Promise(resolve => fixture.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
