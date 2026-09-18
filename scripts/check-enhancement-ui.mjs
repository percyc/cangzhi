// Run against an isolated Next.js harness containing KnowledgeEnhancement at /
// and the real settings page at /settings/enhancement. Every API call is mocked.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.ENHANCEMENT_TEST_URL || 'http://127.0.0.1:3107';
assert.equal(new URL(base).hostname, '127.0.0.1', 'Use an isolated local harness');
const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH});
try {
  for (const width of [1280, 375]) {
    const page = await browser.newPage({viewport: {width, height: 900}});
    const failures = [];
    page.on('pageerror', error => failures.push(error.message));
    let settings = {enabled: false, modules: [], call_budget: 8, cost_acknowledged: false,
      effective_at: null, supported_modules: ['chapter', 'graph']};
    let run = null;
    let creates = 0, resumes = 0;
    const sample = {id: 1, document_id: 1, document_version_id: 1, status: 'partial',
      call_budget: 1, calls_used: 1, total_windows: 2, completed_windows: 1, failed_windows: 0,
      total_source_chars: 20, completed_source_chars: 10, lease_until: null,
      last_error: null, created_at: '2026-09-18T10:00:00Z'};
    await page.route('**/api/**', async route => {
      const req = route.request();
      const path = new URL(req.url()).pathname;
      let body;
      if (path === '/api/enhancement/settings') {
        if (req.method() === 'PUT') {
          settings = {...settings, ...req.postDataJSON(), effective_at: sample.created_at};
          assert.equal(settings.cost_acknowledged, true);
          assert.deepEqual(settings.modules, ['chapter']);
        }
        body = settings;
      } else if (path === '/api/documents/1/enhancements') {
        if (req.method() === 'POST') {
          assert.equal(req.postDataJSON().cost_acknowledged, true);
          run = {...sample};
          creates++;
          body = run;
        } else body = {runs: run ? [run] : [], current_version_id: 1};
      } else if (path === '/api/enhancements/1/resume') {
        assert.equal(req.postDataJSON().cost_acknowledged, true);
        resumes++;
        run = {...run, status: 'completed', completed_windows: 2};
        body = run;
      } else if (path === '/api/enhancements/1/cancel') {
        run = {...run, status: 'cancelled'};
        body = run;
      } else if (path === '/api/enhancements/1') {
        body = {run, total_windows: 2, next_offset: null, windows: [{ordinal: 0, status: 'completed',
          source_segments: [{id: 0, block_id: 'b1', start: 0, stop: 10, page: 1, heading_path: [], text: '可核对的原始证据'}],
          result: {summary: {text: '窗口摘要', evidence_ids: [0]}, entities: [], relations: [], events: [],
                   evidence_status: 'model_extracted_unverified'}}]};
      } else throw new Error(`Unexpected API: ${path}`);
      await route.fulfill({json: body});
    });
    await page.goto(base + '/settings/enhancement');
    await page.getByRole('checkbox', {name: /启用知识增强/}).check();
    await page.getByRole('checkbox', {name: /章节理解/}).check();
    await page.getByRole('checkbox', {name: /我确认开启后/}).check();
    await page.getByRole('button', {name: '保存设置'}).click();
    await page.getByRole('status').filter({hasText: '已开启'}).waitFor();
    await page.goto(base);
    await page.locator('summary').filter({hasText: '知识增强'}).click();
    await page.getByRole('button', {name: '确认额外费用与内容外发', exact: true}).click();
    await page.getByRole('button', {name: '开始增强'}).click();
    await page.getByRole('checkbox', {name: '确认追加模型调用费用与内容外发'}).waitFor();
    assert.equal(creates, 1);
    await page.locator('summary').filter({hasText: '窗口 1'}).click();
    await page.locator('summary').filter({hasText: '摘要依据'}).click();
    await page.getByText('可核对的原始证据', {exact: false}).waitFor();
    await page.getByRole('checkbox', {name: '确认追加模型调用费用与内容外发'}).check();
    await page.getByRole('button', {name: '确认并继续'}).click();
    await page.getByText('状态：已完成').waitFor();
    assert.equal(resumes, 1);
    run = {...sample, status: 'queued'};
    await page.reload();
    await page.locator('summary').filter({hasText: '知识增强'}).click();
    await page.getByRole('button', {name: '取消运行（保留已完成窗口）'}).click();
    await page.getByText('状态：已取消').waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(failures, []);
    await page.close();
    console.log(`enhancement UI: ${width}px settings/start/evidence/resume/cancel passed`);
  }
} finally {
  await browser.close();
}
