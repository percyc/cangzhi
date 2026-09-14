// Start dev: cd apps/web && API_URL=http://127.0.0.1:18080 npm run dev -- --hostname 127.0.0.1 --port 13100
// This test owns a mock auth API on 18080 and intercepts all inbox requests.
// PLAYWRIGHT_MODULE=/path/to/playwright node scripts/test-inbox-browser.cjs
const assert = require('node:assert/strict');
const http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined });
  const page = await browser.newPage();
  const mock = http.createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(req.url.includes('auth/status')
      ? { authenticated: true, setup_required: false, username: 'test' }
      : req.url.includes('current') ? { slug: 'environment', name: '环评', status: 'active' } : []));
  });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let mode = 'normal';
  const calls = [];
  let repairIds = [];
  const stage = { status: 'completed', message: '已完成' };
  await page.route('**/api/documents/inbox?**', async (route) => {
    const url = new URL(route.request().url());
    const filter = url.searchParams.get('filter');
    const offset = Number(url.searchParams.get('offset'));
    calls.push({ filter, offset, started: Date.now() });
    if (mode === 'timeout') return; // Browser must cancel this request itself.
    if (mode === 'slow') await new Promise((resolve) => setTimeout(resolve, 4500));
    const total = filter === 'failed' ? 2 : 60;
    const items = Array.from({ length: Math.max(0, Math.min(25, total - offset)) }, (_, i) => ({
      id: offset + i + 1, title: `${filter}-report-${offset + i + 1}`,
      source_type: 'file', updated_at: '2026-09-14', primary_category: null, origin: null,
      pipeline: { overall_status: 'processing', keyword_searchable: true, vector_searchable: false,
        stages: { parsing: stage, understanding: stage, chunking: { ...stage, child_chunks: 10 },
          embedding: { status: 'pending', message: '缺失向量', completed: 0, total: 10, missing: 10, failed: 0 } } },
    }));
    await route.fulfill({ json: { items, total, has_processing: true,
      counts: { all: 60, attention: 60, processing: 58, failed: 2, needs_organization: 0,
        source_issue: 0, not_vectorized: 60, completed: 0 } } }).catch(() => {});
  });
  await page.route('**/api/documents/batch/repair-vectors', async (route) => {
    repairIds = route.request().postDataJSON().document_ids;
    await route.fulfill({ json: { enqueued: repairIds.length, reset: 0 } });
  });
  try {
    await new Promise((resolve, reject) => {
      mock.once('error', reject);
      mock.listen(18080, '127.0.0.1', resolve);
    });
    await page.goto('http://127.0.0.1:13100/inbox');
    await page.getByRole('link', { name: 'attention-report-1', exact: true }).waitFor();
    assert.equal(await page.locator('tbody tr').count(), 25);
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await page.getByRole('link', { name: 'attention-report-26', exact: true }).waitFor();
    assert.equal(calls.at(-1).offset, 25);
    await page.getByRole('button', { name: '补建当前页缺失向量', exact: true }).click();
    await page.getByText('已补建 25 项缺失向量', { exact: false }).waitFor();
    assert.deepEqual(repairIds, Array.from({ length: 25 }, (_, i) => i + 26));

    mode = 'slow';
    await page.getByRole('button', { name: '全部（60）', exact: true }).click();
    await page.waitForTimeout(100);
    await page.getByRole('button', { name: '失败（2）', exact: true }).click();
    await page.getByRole('link', { name: 'failed-report-1', exact: true }).waitFor();
    assert.equal(await page.locator('tbody tr').count(), 2);
    const before = calls.length;
    await page.waitForTimeout(6500); // One slow poll; no 3-second overlapping poll.
    assert.equal(calls.length, before + 1);
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const hiddenCount = calls.length;
    await page.waitForTimeout(3500);
    assert.equal(calls.length, hiddenCount);
    mode = 'normal';
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForResponse((response) => response.url().includes('/documents/inbox?'));

    mode = 'timeout';
    await page.clock.install();
    await page.getByRole('button', { name: '全部（60）', exact: true }).click();
    await page.waitForTimeout(100);
    await page.clock.fastForward(21000);
    await page.getByRole('alert').filter({ hasText: '超过 20 秒' }).waitFor();
    mode = 'normal';
    await page.getByRole('button', { name: '重新加载', exact: true }).click();
    await page.getByRole('link', { name: 'all-report-1', exact: true }).waitFor();
    assert.equal(await page.getByRole('alert').filter({ hasText: '超过 20 秒' }).count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: pagination, page-scoped repair, stale response, non-overlapping polling, visibility pause, timeout/retry');
  } finally {
    await browser.close();
    await new Promise((resolve) => mock.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
