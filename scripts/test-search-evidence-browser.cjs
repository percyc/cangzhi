// Local mocked UI only. Start web with API_URL=http://127.0.0.1:18183 on port13103.
const assert = require('node:assert/strict');
const http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const server = http.createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(req.url.includes('auth/status')
      ? { authenticated: true, setup_required: false, username: 'test' }
      : req.url.includes('current') ? { id: 1, slug: 'default', name: '默认空间', status: 'active' } : []));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(18183, '127.0.0.1', resolve);
  });
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined });
  try {
    for (const width of [1280, 375]) {
      const page = await browser.newPage({ viewport: { width, height: 800 } });
      const errors = [];
      page.on('pageerror', (error) => errors.push(error.message));
      let legacy = false;
      const chunk = { id: 10, parent_id: null, type: 'paragraph', heading_path: [],
        page: 2, paragraph_index: 1, source_start: 0, source_end: 50 };
      await page.route('**/api/search**', async (route) => {
        if (route.request().url().includes('/filters')) {
          await route.fulfill({ json: { categories: [], tags: [], source_types: [] } });
          return;
        }
        await route.fulfill({ json: { query: 'test', total: 1, limit: 20, offset: 0,
          backend: 'hybrid', hits: [{ document_id: 1, document_version_id: 2,
            title: '测试文档', source_type: 'file', source_url: null, score: 1,
            snippet: '原有主证据保留', highlights: [], chunk, categories: [], tags: [],
            ...(legacy ? {} : { supporting_evidence: [{ document_id: 1,
              document_version_id: 2, chunk: { ...chunk, id: 20, page: 9 },
              snippet: '补充片段', context: '补充原文内容\n<img src=x onerror=alert(1)>\n' + '相关证据。'.repeat(150) }] }),
          }] } });
      });
      await page.goto('http://127.0.0.1:13103/search?q=test');
      const details = page.locator('details').filter({ hasText: '同文档的补充证据' });
      await details.waitFor();
      assert.equal(await details.getAttribute('open'), null);
      await details.locator('summary').click();
      assert.equal(await details.getAttribute('open'), '');
      assert.ok(await details.getByText('补充原文内容', { exact: false }).isVisible());
      assert.equal(await details.locator('img').count(), 0);
      const link = details.getByRole('link', { name: '查看来源文档' });
      assert.equal(await link.getAttribute('href'), '/documents/1?chunk_id=20&page=9');
      assert.ok(await page.getByText('原有主证据保留').isVisible());
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      await details.locator('summary').click();
      assert.equal(await details.getAttribute('open'), null);
      legacy = true;
      await page.reload();
      await page.getByText('原有主证据保留').waitFor();
      assert.equal(await page.locator('details').filter({ hasText: '同文档的补充证据' }).count(), 0);
      assert.deepEqual(errors, []);
      await page.close();
      console.log(`PASS ${width}px: collapsed support, expand/collapse, exact source link, escaped HTML, legacy response`);
    }
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
