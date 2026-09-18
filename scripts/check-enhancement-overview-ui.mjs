// Isolated harness only; synthetic API responses, no production or model calls.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.ENHANCEMENT_TEST_URL || 'http://127.0.0.1:3107';
assert.equal(new URL(base).hostname, '127.0.0.1');
const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH});
try {
  for (const width of [1280, 375]) {
    const page = await browser.newPage({viewport: {width, height: 900}});
    const errors = [], requests = [];
    page.on('pageerror', err => errors.push(err.message));
    let failChild = false, slowChild = false;
    const run = {id: 1, document_id: 1, document_version_id: 1, status: 'completed',
      call_budget: 8, calls_used: 8, total_windows: 5, completed_windows: 5, failed_windows: 0,
      total_source_chars: 50, completed_source_chars: 50, last_error: null, lease_until: null,
      hierarchy: {enabled: true, total_nodes: 3, completed_nodes: 3, root_key: 'L2:0', status: 'completed'}};
    let settings = {enabled: true, modules: ['chapter'], call_budget: 8, cost_acknowledged: true,
      supported_modules: ['chapter', 'graph', 'overview']};
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url());
      requests.push([req.method(), url.pathname]);
      let body;
      if (url.pathname === '/api/enhancement/settings') {
        if (req.method() === 'PUT') {
          settings = {...settings, ...req.postDataJSON()};
          assert(settings.modules.includes('chapter') && settings.modules.includes('overview'));
        }
        body = settings;
      } else if (url.pathname === '/api/documents/1/enhancements') {
        body = {current_version_id: 1, runs: [run]};
      } else if (url.pathname === '/api/enhancements/1') {
        body = {run, total_windows: 5, next_offset: null, windows: [{ordinal: 0, status: 'completed',
          result: {summary: {text: '局部解释', evidence_ids: [0]}, entities: [], relations: [], events: []},
          source_segments: [{id: 0, text: '可核对的原始事实', block_id: 'b1', start: 0, stop: 9, page: 1, heading_path: []}]}]};
      } else if (url.pathname === '/api/enhancements/1/overview') {
        const child = url.searchParams.has('node_key');
        if (child && slowChild) await new Promise(resolve => setTimeout(resolve, 400));
        if (child && failChild) {
          await route.fulfill({status: 409, json: {detail: {code: 'enhancement_stale', message: '原文已变化'}}});
          return;
        }
        body = {run, enabled: true, read_only: true, model_calls: 0, node: {
          node_key: child ? 'L1:0' : 'L2:0', level: child ? 1 : 2, status: 'completed',
          window_start: 0, window_stop: child ? 4 : 5,
          result: {summary: {text: child ? '分组概览' : '根概览', support_refs: [child ? 'w:0' : 'n:L1:0']}},
          children: [{ref: child ? 'w:0' : 'n:L1:0', kind: child ? 'window' : 'node',
            node_key: child ? null : 'L1:0', window_index: child ? 0 : null,
            status: 'completed', summary: {text: '子结果', evidence_ids: child ? [0] : []}, entities: []}],
          evidence_status: 'model_extracted_unverified'}};
      } else throw new Error(`Unexpected API ${url.pathname}`);
      await route.fulfill({json: body});
    });
    await page.goto(base + '/settings/enhancement');
    assert.equal(await page.getByRole('checkbox', {name: /分层概览/}).isChecked(), false);
    await page.getByRole('checkbox', {name: /分层概览/}).check();
    await page.getByRole('button', {name: '保存设置'}).click();
    await page.getByRole('status').filter({hasText: '已开启'}).waitFor();
    await page.goto(base);
    await page.locator('summary').filter({hasText: '知识增强'}).click();
    await page.getByRole('button', {name: /分层概览（树形汇总）/}).click();
    await page.getByText('根概览', {exact: true}).waitFor();
    assert(await page.getByText(/窗口 1–5/).isVisible());
    await page.getByRole('button', {name: '下钻查看'}).click();
    await page.getByText('分组概览', {exact: true}).waitFor();
    assert(await page.getByText(/窗口 1–4/).isVisible());
    await page.getByRole('button', {name: '查看窗口证据'}).click();
    await page.locator('summary').filter({hasText: '摘要依据'}).click();
    await page.getByText(/可核对的原始事实/).waitFor();
    await page.getByRole('button', {name: '返回上一层'}).click();
    await page.getByText('根概览', {exact: true}).waitFor();
    failChild = true;
    await page.getByRole('button', {name: '下钻查看'}).click();
    await page.getByRole('alert').filter({hasText: '原文已变化'}).waitFor();
    assert.equal(await page.getByText('根概览', {exact: true}).count(), 0);
    await page.getByRole('button', {name: '返回上一层'}).click();
    await page.getByText('根概览', {exact: true}).waitFor();
    assert.equal(await page.getByRole('alert').filter({hasText: '原文已变化'}).count(), 0);
    failChild = false; slowChild = true;
    await page.getByRole('button', {name: '下钻查看'}).click();
    await page.getByRole('button', {name: '返回上一层'}).click();
    await page.getByText('根概览', {exact: true}).waitFor();
    await page.waitForTimeout(500);
    assert.equal(await page.getByText('分组概览', {exact: true}).count(), 0);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    run.status = 'partial'; run.hierarchy.completed_nodes = 1; run.hierarchy.status = 'pending';
    await page.reload();
    await page.locator('summary').filter({hasText: '知识增强'}).click();
    await page.getByRole('button', {name: /分层概览.*尚未完成/}).waitFor();
    assert(requests.every(([method, path]) => method === 'GET' || path === '/api/enhancement/settings'));
    assert.deepEqual(errors, []);
    console.log(`overview UI ${width}px: drilldown/back/source/errors/race/budget status passed`);
    await page.close();
  }
} finally {
  await browser.close();
}
