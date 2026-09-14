// Isolated page audit. Start page-review-api.py and a Next dev server on 13101
// with API_URL=http://127.0.0.1:18081. Playwright paths can be supplied via env.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), 'cangzhi-pages-'));
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined });
  const context = await browser.newContext();
  const password = crypto.randomBytes(18).toString('hex');
  const report = [];
  try {
    const fresh = await context.newPage();
    for (const [device, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
      await fresh.setViewportSize(viewport);
      await fresh.goto('http://127.0.0.1:13101/setup', { waitUntil: 'networkidle' });
      await fresh.locator('form').waitFor();
      assert.equal(new URL(fresh.url()).pathname, '/setup');
      await fresh.screenshot({ path: path.join(artifacts, `${device}-setup-fresh.png`), fullPage: true });
      const overflow = await fresh.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      report.push({ device, route: '/setup#fresh', status: 200, overflow, errors: [] });
    }
    await fresh.close();
    const setup = await context.request.post('http://127.0.0.1:18081/api/auth/setup', {
      data: { username: 'page-review', password },
    });
    assert.equal(setup.status(), 200);
    const noteResponse = await context.request.post('http://127.0.0.1:18081/api/notes', {
      data: { title: '页面验收示例', content: '# 页面验收\n\n这是隔离环境中的测试资料。\n\n- 第一条\n- 第二条' },
    });
    assert.equal(noteResponse.status(), 201);
    const note = await noteResponse.json();
    const routes = ['/', '/documents', `/documents/${note.id}`, '/inbox', '/search', '/ask',
      '/tags', '/categories', '/links/new', '/notes/new', `/notes/${note.id}/edit`, '/files/upload',
      '/processing', '/sources', '/settings', '/settings?section=embedding', '/settings?section=ocr',
      '/settings/sources', '/settings/access', '/settings/data', '/settings/workspaces'];
    for (const [device, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
      const page = await context.newPage();
      await page.setViewportSize(viewport);
      let errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('response', response => {
        if (response.status() >= 500) errors.push(`${response.status()} ${new URL(response.url()).pathname}`);
      });
      for (const route of routes) {
        errors = [];
        const start = Date.now();
        const response = await page.goto(`http://127.0.0.1:13101${route}`, { waitUntil: 'networkidle' });
        await page.waitForTimeout(200);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        const heading = await page.locator('h1').allTextContents();
        const file = `${device}-${route.replace(/[^a-z0-9]/gi, '_') || 'root'}.png`;
        await page.screenshot({ path: path.join(artifacts, file), fullPage: true });
        report.push({ device, route, status: response?.status(), finalPath: new URL(page.url()).pathname,
          heading, overflow, errors: [...errors], milliseconds: Date.now() - start, screenshot: file });
        console.log(JSON.stringify(report.at(-1)));
      }
      await page.goto('http://127.0.0.1:13101/documents', { waitUntil: 'networkidle' });
      await page.getByRole('button', { name: '回收站', exact: true }).click();
      await page.getByText('回收站为空', { exact: true }).waitFor();
      report.push({ device, route: '/documents#trash', status: 200, overflow: 0, errors: [] });
      await page.close();
      const anonymous = await browser.newContext({ viewport });
      const login = await anonymous.newPage();
      await login.goto('http://127.0.0.1:13101/login', { waitUntil: 'networkidle' });
      await login.screenshot({ path: path.join(artifacts, `${device}-login.png`), fullPage: true });
      const overflow = await login.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      report.push({ device, route: '/login', status: 200, overflow, errors: [] });
      await login.goto('http://127.0.0.1:13101/setup', { waitUntil: 'networkidle' });
      assert.equal(new URL(login.url()).pathname, '/login');
      report.push({ device, route: '/setup', status: 200, finalPath: '/login', overflow: 0, errors: [] });
      await login.locator('#username').fill('page-review');
      await login.locator('#password').fill(password);
      await login.getByRole('button', { name: '登录', exact: true }).click();
      await login.waitForURL('**/documents');
      report.push({ device, route: '/login#submit', status: 200, overflow: 0, errors: [] });
      await anonymous.close();
    }
    fs.writeFileSync(path.join(artifacts, 'report.json'), JSON.stringify(report, null, 2));
    console.log(`ARTIFACTS ${artifacts}`);
    assert.equal(report.filter(row => row.status >= 400 || row.errors.length || row.overflow > 2).length, 0,
      'See report for page errors or horizontal overflow');
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
