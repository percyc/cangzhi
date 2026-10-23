/* eslint-disable @typescript-eslint/no-require-imports -- Isolated Chromium acceptance with synthetic configuration only. */
// No deployed service, real key or upstream model is used. The fixture simulates
// persistence; server-side encryption/copy semantics are covered by API tests.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

const initial = {
  provider: 'disabled', openai_base_url: '', openai_model: '', has_api_key: false,
  ollama_base_url: 'http://localhost:11434', ollama_model: '', timeout_seconds: 30,
  prompt_version: 'synthetic', updated_at: null,
  embedding_provider: 'openai', embedding_base_url: 'https://old-vector.invalid/v1',
  embedding_model: 'legacy-embedding', has_embedding_api_key: true, embedding_timeout_seconds: 30,
  ocr_provider: 'openai', ocr_base_url: 'https://old-vision.invalid/v1', ocr_model: 'legacy-vision',
  has_ocr_api_key: true, ocr_timeout_seconds: 30, ocr_confidence_threshold: 600,
  ocr_min_chars: 8, ocr_max_external_pages: 20,
};
let config = structuredClone(initial), failDiscovery = false, failSave = false, failRead = false;
const requests = [];
const fixture = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture.test');
  let raw = ''; for await (const chunk of req) raw += chunk;
  const body = raw ? JSON.parse(raw) : {};
  requests.push({ method: req.method, path: url.pathname, body });
  const json = (value, code = 200) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
  const route = url.pathname;
  if (route === '/api/auth/status') return json({ authenticated: true, setup_required: false, admin: { id: 1, username: 'synthetic-admin' } });
  if (route === '/api/workspaces/current') return json({ slug: 'default', name: '合成验收空间', status: 'active' });
  if (route === '/api/workspaces') return json([{ slug: 'default', name: '合成验收空间', status: 'active' }]);
  if (route === '/api/system/version') return json({ version: 'synthetic', revision: 'synthetic', built_at: '2026-10-23T00:00:00Z', dirty: false });
  if (route === '/api/system/upload-settings') return json({ max_upload_size_mb: 50 });
  if (route === '/api/embeddings/status') return json({ active_profile: { id: null, status: null, model: null, dim: null, provider: null }, profiles: [] });
  if (route === '/api/settings/ai') {
    if (req.method === 'GET' && failRead) {
      await new Promise(resolve => setTimeout(resolve, 600));
      return json({ detail: '合成配置读取失败' }, 503);
    }
    if (req.method === 'PATCH') {
      if (failSave) { failSave = false; return json({ detail: '合成保存失败，请重试' }, 503); }
      config.provider = body.provider;
      if (body.openai) { config.openai_base_url = body.openai.base_url; config.openai_model = body.openai.model; }
      if (body.api_key_action === 'replace') config.has_api_key = true;
      if (body.api_key_action === 'clear') config.has_api_key = false;
      config.timeout_seconds = body.timeout_seconds;
    }
    return json({ config });
  }
  if (route === '/api/settings/ai/embedding' || route === '/api/settings/ai/ocr') {
    const channel = route.endsWith('/embedding') ? 'embedding' : 'ocr';
    config[`${channel}_provider`] = body.provider;
    config[`${channel}_base_url`] = body.reuse_chat ? config.openai_base_url : body.base_url;
    config[`${channel}_model`] = body.model;
    config[`${channel}_timeout_seconds`] = body.timeout_seconds;
    if (body.reuse_chat || body.api_key_action === 'replace') config[channel === 'embedding' ? 'has_embedding_api_key' : 'has_ocr_api_key'] = true;
    if (channel === 'ocr') {
      config.ocr_confidence_threshold = body.confidence_threshold;
      config.ocr_min_chars = body.min_chars;
      config.ocr_max_external_pages = body.max_external_pages;
    }
    return json({ config });
  }
  if (route === '/api/settings/ai/models/discover') return failDiscovery
    ? json({ detail: '此合成服务没有模型列表，请手动输入' }, 503)
    : json({ models: [{ id: 'fixture-chat' }, { id: 'fixture-embedding' }, { id: 'fixture-vision' }], provider: 'openai', current_model: '' });
  if (route === '/api/settings/ai/test') return json({ ok: true, message: 'synthetic ok' });
  if (route === '/api/settings/ai/ocr/test') return json({ ok: true, message: '合成 OCR 测试通过' });
  if (route === '/api/embeddings/test') return json({ ok: true, decision: 'unknown', reason: 'synthetic', profile: { id: null, status: null }, scores: [] });
  return json({ detail: `Unmocked fixture route: ${route}` }, 404);
});
const writes = () => requests.filter(request => request.method !== 'GET');
const patches = () => requests.filter(request => request.method === 'PATCH');
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate, message) {
  for (let index = 0; index < 120; index++) { if (await predicate()) return; await delay(250); }
  throw new Error(message);
}

async function main() {
  const apiPort = await listen(fixture);
  const reservation = http.createServer(), port = await listen(reservation);
  await new Promise(resolve => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(process.execPath, [require.resolve('next/dist/bin/next'), 'dev', '--hostname', '127.0.0.1', '--port', String(port)], {
    cwd: path.resolve(__dirname, '../..'), env: { ...process.env, API_URL: `http://127.0.0.1:${apiPort}`, NEXT_TELEMETRY_DISABLED: '1', CANGZHI_WEB_BASE_PATH: '' }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '', browser;
  server.stdout.on('data', data => { log = (log + data).slice(-12000); });
  server.stderr.on('data', data => { log = (log + data).slice(-12000); });
  try {
    await until(async () => { try { return (await fetch(`${base}/api/auth/status`)).ok; } catch { return false; } }, 'Isolated Next server did not start');
    browser = await chromium.launch({ headless: true, executablePath: ['/usr/bin/google-chrome', '/usr/bin/chromium'].find(file => fs.existsSync(file)) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    const steps = () => page.getByRole('region', { name: '配置步骤' });
    const step = title => steps().getByRole('button').filter({ hasText: title }).first();
    const search = () => page.getByRole('region', { name: '设置与功能搜索' });
    const next = () => page.getByRole('button', { name: /下一步（也可暂不配置）/ });

    await page.goto(`${base}/settings`);
    await page.getByRole('link', { name: '打开配置引导 →' }).click();
    await page.getByRole('heading', { name: '配置引导', exact: true }).waitFor();
    assert.equal(await steps().getByRole('button').count(), 4);
    await next().click(); await next().click();
    await page.getByRole('button', { name: '查看配置摘要 →' }).click();
    await page.getByRole('heading', { name: '核对配置，开始使用' }).waitFor();
    assert.equal(writes().length, 0, 'Opening/skipping must never save, test or build');
    assert.deepEqual(config, initial, 'Skip retains existing channels');
    await page.getByText('尚无已启用的向量索引，关键词检索仍可用。').waitFor();

    await step('对话与整理').click();
    await page.getByRole('button', { name: /OpenAI 兼容 支持 OpenAI/ }).click();
    await page.locator('#openai-base-url').fill('https://shared-model.invalid/v1');
    await page.locator('#api-key').fill('synthetic-only-not-a-real-secret');
    await page.locator('#openai-model').fill('chat-draft');
    await next().click();
    await page.getByRole('alert').filter({ hasText: '此步骤有未保存的修改' }).waitFor();
    await page.getByRole('button', { name: '保留草稿并继续' }).click();
    assert.equal(await page.locator('#embedding-reuse-chat').isDisabled(), true, 'Unsaved chat cannot be copied');
    await step('对话与整理').click();
    assert.equal(await page.locator('#openai-model').inputValue(), 'chat-draft');
    assert.equal(await page.locator('#api-key').inputValue(), 'synthetic-only-not-a-real-secret');
    await page.getByRole('button', { name: '获取模型列表', exact: true }).click();
    await page.getByLabel('模型名称快速选择', { exact: true }).selectOption('fixture-chat');
    assert.equal(await page.locator('#openai-model').inputValue(), 'fixture-chat');
    failDiscovery = true;
    await page.getByRole('button', { name: '刷新模型列表', exact: true }).click();
    await page.getByText(/此合成服务没有模型列表/).waitFor(); failDiscovery = false;
    await page.locator('#openai-model').fill('manual-chat');
    failSave = true;
    await page.getByRole('button', { name: '保存对话模型', exact: true }).click();
    await page.getByText('合成保存失败，请重试', { exact: true }).waitFor();
    assert.equal(config.provider, 'disabled'); assert.equal(await page.locator('#openai-model').inputValue(), 'manual-chat');
    await page.getByRole('button', { name: '保存对话模型', exact: true }).click();
    await until(async () => await page.getByRole('button', { name: '保存对话模型', exact: true }).isDisabled(), 'Chat save did not settle');
    assert.equal(config.openai_model, 'manual-chat'); assert.equal(config.embedding_model, initial.embedding_model); assert.equal(config.ocr_model, initial.ocr_model);
    assert.ok(patches().every(request => request.path === '/api/settings/ai'));
    assert.ok(patches().every(request => !Object.keys(request.body).some(key => key.startsWith('embedding') || key.startsWith('ocr'))));
    assert.equal(await page.locator('#api-key').inputValue(), '', 'Saved key is cleared from form');
    await page.getByRole('button', { name: '测试对话连接', exact: true }).click();
    await page.getByText('对话模型连接正常', { exact: true }).waitFor();

    await page.locator('#openai-base-url').fill('https://unsaved-draft.invalid/v1');
    await next().click(); await page.getByRole('button', { name: '保留草稿并继续' }).click();
    await page.locator('#embedding-base-url').fill('https://independent-draft.invalid/v1');
    await page.locator('#embedding-reuse-chat').check();
    assert.equal(await page.locator('#embedding-base-url').inputValue(), 'https://shared-model.invalid/v1');
    assert.equal(await page.locator('#embedding-base-url').isDisabled(), true);
    assert.equal(await page.locator('#embedding-api-key').count(), 0);
    assert.equal(await page.locator('#embedding-model').inputValue(), 'legacy-embedding', 'Copy does not choose chat model');
    await page.locator('#embedding-reuse-chat').uncheck();
    assert.equal(await page.locator('#embedding-base-url').inputValue(), 'https://independent-draft.invalid/v1');
    await page.locator('#embedding-reuse-chat').check();
    await page.getByRole('button', { name: '获取模型列表', exact: true }).click();
    await page.getByLabel('Embedding 模型快速选择').selectOption('fixture-embedding');
    const discovery = writes().at(-1);
    assert.equal(discovery.path, '/api/settings/ai/models/discover'); assert.equal(discovery.body.channel, 'chat'); assert.equal(discovery.body.base_url, 'https://shared-model.invalid/v1');
    assert.equal(discovery.body.api_key, undefined);
    await page.getByRole('button', { name: '保存向量模型', exact: true }).click();
    await page.getByText('向量设置已保存。下一步请测试兼容性；不会自动构建索引。').waitFor();
    const vectorPatch = patches().at(-1);
    assert.equal(vectorPatch.path, '/api/settings/ai/embedding'); assert.equal(vectorPatch.body.reuse_chat, true);
    assert.equal(vectorPatch.body.model, 'fixture-embedding'); assert.equal(vectorPatch.body.base_url, undefined); assert.equal(vectorPatch.body.api_key, undefined);
    await page.getByRole('button', { name: '测试向量兼容性', exact: true }).click();
    await page.getByText('首次使用：连接正常，后续需要创建首个向量索引').waitFor();
    await step('对话与整理').click();
    assert.equal(await page.locator('#openai-base-url').inputValue(), 'https://unsaved-draft.invalid/v1');
    await page.locator('#openai-base-url').fill('https://shared-model.invalid/v1');
    await step('图片文字识别').click();
    await page.locator('#ocr-reuse-chat').check();
    assert.equal(await page.locator('#ocr-base-url').inputValue(), 'https://shared-model.invalid/v1');
    assert.equal(await page.locator('#ocr-api-key').count(), 0);
    assert.equal(await page.locator('#ocr-model').inputValue(), 'legacy-vision');
    await page.locator('#ocr-model').fill('manual-vision');
    await page.getByRole('button', { name: '保存外部 OCR', exact: true }).click();
    await page.getByText('外部 OCR 设置已保存', { exact: true }).waitFor();
    const ocrPatch = patches().at(-1);
    assert.equal(ocrPatch.path, '/api/settings/ai/ocr'); assert.equal(ocrPatch.body.reuse_chat, true);
    assert.equal(ocrPatch.body.model, 'manual-vision'); assert.equal(ocrPatch.body.base_url, undefined); assert.equal(ocrPatch.body.api_key, undefined);
    await page.getByRole('button', { name: '测试实际 OCR 识别', exact: true }).click();
    await page.getByText('合成 OCR 测试通过').waitFor();
    await step('对话与整理').click();
    await page.locator('#openai-base-url').fill('https://new-chat.invalid/v1');
    await page.locator('#api-key').fill('another-synthetic-only-value');
    await page.getByRole('button', { name: '保存对话模型', exact: true }).click();
    await until(() => config.openai_base_url === 'https://new-chat.invalid/v1', 'Updated chat not saved');
    await until(async () => await page.getByRole('button', { name: '保存对话模型', exact: true }).isDisabled(), 'Updated chat draft did not settle');
    assert.equal(config.embedding_base_url, 'https://shared-model.invalid/v1'); assert.equal(config.ocr_base_url, 'https://shared-model.invalid/v1');
    assert.equal(writes().filter(request => /\/embeddings\/profiles\//.test(request.path)).length, 0);

    const requestCount = requests.length;
    for (const [query, href] of [['修改密码', '/settings/account'], ['上传太大', '/settings/uploads'], ['添加数据库', '/settings/sources'], ['EMBEDDING', '/settings?section=embedding'], ['模型怎么配置', '/settings/setup']]) {
      await search().getByRole('searchbox').fill(query);
      assert.equal(await search().getByRole('link').first().getAttribute('href'), href);
    }
    await search().getByRole('searchbox').fill('不存在的功能xyz123');
    await search().getByText('没有匹配的入口').waitFor();
    assert.equal(await search().getByRole('link').count(), 0);
    assert.equal(requests.length, requestCount, 'Typing search must not request config, content or AI');
    await search().getByRole('searchbox').fill('模型');
    await search().getByRole('searchbox').press('ArrowDown');
    assert.equal(await search().getByRole('link').first().evaluate(element => element === document.activeElement), true);
    await page.keyboard.press('Escape');
    assert.equal(await search().getByRole('searchbox').inputValue(), '');

    for (const viewport of [{ width: 1440, height: 1050 }, { width: 1024, height: 480 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      if (viewport.width === 390) {
        const toggle = page.getByRole('button', { name: '查找或切换设置' });
        assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
        assert.equal(await search().isVisible(), false, 'Mobile tools default to collapsed');
        await toggle.click();
        assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
        await search().getByRole('searchbox').fill('修改密码');
        await toggle.click();
        assert.equal(await search().isVisible(), false);
        await toggle.click();
        assert.equal(await search().getByRole('searchbox').inputValue(), '修改密码', 'Collapse retains query');
        assert.equal(await search().getByRole('link').first().getAttribute('href'), '/settings/account');
      }
      await search().getByRole('searchbox').fill('模型');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `overflow at ${viewport.width}`);
      const summary = page.locator('#system-version > summary');
      await summary.scrollIntoViewIfNeeded();
      assert.equal(await summary.evaluate(element => { const box = element.getBoundingClientRect(); return element.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)); }), true, `Version obscured at ${viewport.width}`);
      await search().getByRole('searchbox').fill('');
      if (viewport.width === 390) await page.getByRole('button', { name: '查找或切换设置' }).click();
      await page.evaluate(() => { document.querySelector('.settings-rail-links').scrollTop = 0; window.scrollTo(0, 0); });
      if (viewport.width === 1440 || viewport.width === 390) await page.screenshot({ path: `/tmp/cangzhi-guided-${viewport.width === 1440 ? 'desktop' : 'mobile'}.png`, fullPage: true });
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('#openai-model').fill('unsaved-model');
    await search().getByRole('searchbox').fill('上传上限');
    let dialogs = 0;
    page.once('dialog', async dialog => { dialogs++; await dialog.dismiss(); });
    await search().getByRole('link').first().click();
    assert.equal(dialogs, 1); assert.ok(page.url().endsWith('/settings/setup'));
    assert.equal(await page.locator('#openai-model').inputValue(), 'unsaved-model');
    page.once('dialog', async dialog => { dialogs++; await dialog.accept(); });
    await search().getByRole('searchbox').press('Enter');
    await page.waitForURL('**/settings/uploads');
    assert.equal(dialogs, 2, 'Exactly one confirmation per navigation');
    await page.locator('#upload-limit').fill('123');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '查找或切换设置' }).click();
    page.once('dialog', async dialog => { dialogs++; await dialog.dismiss(); });
    await page.getByLabel('切换设置页面').selectOption('setup');
    assert.equal(dialogs, 3); assert.ok(page.url().endsWith('/settings/uploads')); assert.equal(await page.locator('#upload-limit').inputValue(), '123');
    await page.locator('#upload-limit').fill('50');
    await page.getByLabel('切换设置页面').selectOption('setup');
    await page.waitForURL('**/settings/setup');
    await page.locator('#openai-model').waitFor();
    assert.equal(await page.locator('#openai-model').inputValue(), 'manual-chat');

    // A copy draft can outlive its saved source. The user must still be able to
    // cancel copying and configure the independent destination without a trap.
    await step('语义检索').click();
    await page.locator('#embedding-reuse-chat').check();
    await step('对话与整理').click();
    await page.getByRole('button', { name: '保留草稿并继续' }).click();
    await page.getByRole('button', { name: /不使用 资料会正常入库/ }).click();
    await page.getByRole('button', { name: '保存对话模型', exact: true }).click();
    await until(async () => await page.getByRole('button', { name: '保存对话模型', exact: true }).isDisabled(), 'Disabling chat did not settle');
    assert.equal(config.provider, 'disabled');
    await step('语义检索').click();
    assert.equal(await page.locator('#embedding-reuse-chat').isChecked(), true);
    assert.equal(await page.locator('#embedding-reuse-chat').isDisabled(), false, 'Invalid source must not trap checked copy');
    assert.equal(await page.getByRole('button', { name: '获取模型列表', exact: true }).isDisabled(), true);
    await page.locator('#embedding-reuse-chat').uncheck();
    await page.locator('#embedding-model').fill('independent-again');
    await page.getByRole('button', { name: '保存向量模型', exact: true }).click();
    await page.getByText('向量设置已保存。下一步请测试兼容性；不会自动构建索引。').waitFor();
    assert.equal(patches().at(-1).body.reuse_chat, undefined);
    assert.equal(config.embedding_model, 'independent-again');

    // Settings routes share one navigation instance, not just matching markup.
    await page.setViewportSize({ width: 1440, height: 800 });
    const nav = () => page.getByRole('navigation', { name: '设置分类', exact: true });
    await search().getByRole('searchbox').fill('上传上限');
    await page.locator('#system-version > summary').click();
    await page.getByText('服务 API · synthetic', { exact: true }).waitFor();
    await page.evaluate(() => {
      window.__settingsRail = document.querySelector('.settings-rail');
      window.__settingsSearch = document.querySelector('.settings-rail input[type="search"]');
      window.__settingsVersion = document.querySelector('#system-version');
    });
    const versionReads = requests.filter(request => request.path === '/api/system/version').length;
    const beforeNavigationWrites = writes().length;
    await search().getByRole('link').first().click();
    await page.waitForURL('**/settings/uploads');
    await page.locator('#upload-limit').waitFor();
    assert.equal(await search().getByRole('searchbox').inputValue(), '上传上限');
    assert.equal(await page.evaluate(() => window.__settingsRail === document.querySelector('.settings-rail') && window.__settingsSearch === document.querySelector('.settings-rail input[type="search"]') && window.__settingsVersion === document.querySelector('#system-version')), true);
    assert.equal(await page.locator('#system-version').evaluate(element => element.open), true);
    assert.equal(requests.filter(request => request.path === '/api/system/version').length, versionReads);
    await page.locator('#system-version > summary').click();
    await search().getByRole('searchbox').fill('');
    await nav().getByRole('link', { name: /数据与备份/ }).scrollIntoViewIfNeeded();
    const scroll = await page.locator('.settings-rail-links').evaluate(element => element.scrollTop);
    assert.ok(scroll > 0, 'Exercise actual sidebar scrolling');
    await nav().getByRole('link', { name: /数据与备份/ }).click();
    await page.waitForURL('**/settings/data');
    assert.equal(await page.locator('.settings-rail-links').evaluate(element => element.scrollTop), scroll);
    const historyLength = await page.evaluate(() => history.length);
    await nav().getByRole('link', { name: /数据与备份/ }).click();
    assert.equal(await page.evaluate(() => history.length), historyLength);
    await page.goBack(); await page.waitForURL('**/settings/uploads');
    await page.goForward(); await page.waitForURL('**/settings/data');
    assert.equal(await nav().locator('[aria-current="page"]').getAttribute('href'), '/settings/data');
    assert.equal(await page.evaluate(() => window.__settingsRail === document.querySelector('.settings-rail')), true);
    await nav().getByRole('link', { name: /对话模型/ }).click();
    await page.locator('#openai-model').waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: /OpenAI 兼容 支持 OpenAI/ }).click();
    await page.locator('#openai-model').fill('navigation-draft');
    await nav().getByRole('link', { name: /图片文字识别/ }).click();
    await page.waitForURL('**/settings?section=ocr');
    await page.locator('#ocr-model').waitFor();
    // Browser traversal retains the existing global dirty-form confirmation.
    page.once('dialog', dialog => dialog.accept());
    await page.goBack();
    await page.waitForURL('**/settings?section=chat');
    await page.locator('#openai-model').waitFor();
    assert.equal(await page.locator('#openai-model').inputValue(), 'navigation-draft');
    page.once('dialog', dialog => dialog.dismiss());
    await nav().getByRole('link', { name: /文件上传/ }).click();
    assert.equal(await page.locator('#openai-model').inputValue(), 'navigation-draft');
    page.once('dialog', dialog => dialog.accept());
    await nav().getByRole('link', { name: /文件上传/ }).click();
    await page.waitForURL('**/settings/uploads');
    assert.equal(writes().length, beforeNavigationWrites, 'Navigation never saves, tests or builds');

    // A failed or slow config read only affects the right pane. Navigation and
    // mobile expansion remain usable, without reviving departed form guards.
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileToggle = page.getByRole('button', { name: '查找或切换设置' });
    if (await mobileToggle.getAttribute('aria-expanded') === 'false') await mobileToggle.click();
    await search().getByRole('searchbox').fill('模型');
    failRead = true;
    await page.getByLabel('切换设置页面').selectOption('chat');
    await page.getByText('设置加载中…', { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.__settingsRail === document.querySelector('.settings-rail')), true);
    await page.getByText(/读取设置失败：/).waitFor();
    assert.equal(await mobileToggle.getAttribute('aria-expanded'), 'true');
    assert.equal(await search().getByRole('searchbox').inputValue(), '模型');
    await page.getByLabel('切换设置页面').selectOption('uploads');
    await page.waitForURL('**/settings/uploads');
    failRead = false;

    assert.equal(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }).includes('synthetic-only')), false);
    assert.deepEqual(errors, []);
    console.log('PASS guided settings: four steps, independent copy/save/test, drafts/guards, persistent navigation/search/scroll/version, history, current-item no-op, slow/failed reads, privacy and 390/1024/1440 layouts');
  } catch (error) { process.stderr.write(log); throw error; }
  finally {
    await browser?.close(); server.kill('SIGTERM');
    fixture.closeAllConnections();
    await new Promise(resolve => fixture.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
