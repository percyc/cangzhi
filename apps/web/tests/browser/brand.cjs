/* eslint-disable @typescript-eslint/no-require-imports -- Isolated HTTP/Chromium acceptance with synthetic responses only. */
// Run serially with other Next tests: both root and gateway builds use .next.
// This suite never accesses a deployed instance, creates an account or calls AI.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

const webRoot = path.resolve(__dirname, '../..');
const outputDirectory = process.env.CANGZHI_BRAND_SCREENSHOT_DIR || fs.mkdtempSync(path.join(os.tmpdir(), 'cangzhi-brand-'));
fs.mkdirSync(outputDirectory, { recursive: true });
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
let phase = 'login';
const requests = [];
const workspace = { slug: 'default', name: '品牌验收空间', description: null, is_default: true, status: 'active', settings: {} };
const fixture = http.createServer((request, response) => {
  const route = new URL(request.url, 'http://fixture.test').pathname;
  requests.push({ method: request.method, path: route });
  const json = (data, status = 200) => { response.writeHead(status, { 'content-type': 'application/json' }); response.end(JSON.stringify(data)); };
  if (route === '/api/auth/status') return json({ authenticated: phase === 'authenticated', setup_required: phase === 'setup', admin: phase === 'authenticated' ? { id: 1, username: 'brand-test' } : null });
  if (route === '/api/auth/setup-status') return json({ setup_required: phase === 'setup', has_admin: phase !== 'setup' });
  if (route === '/api/workspaces') return json([workspace]);
  if (route === '/api/workspaces/current') return json(workspace);
  if (route === '/api/system/upload-settings') return json({ max_upload_size_mb: 50 });
  if (route === '/api/system/version') return json({ version: 'synthetic', revision: 'synthetic', built_at: '2026-10-23T00:00:00Z', dirty: false });
  return json({ detail: `Unmocked fixture route: ${route}` }, 404);
});

async function until(predicate, message) {
  for (let attempt = 0; attempt < 160; attempt++) {
    if (await predicate()) return;
    await delay(250);
  }
  throw new Error(message);
}

async function verifyIcons(page, origin, basePath) {
  const links = await page.locator('link[rel="icon"], link[rel="apple-touch-icon"]').evaluateAll(elements => elements.map(element => ({
    href: element.getAttribute('href'), sizes: element.getAttribute('sizes'), type: element.getAttribute('type'), rel: element.getAttribute('rel'),
  })));
  assert.equal(links.length, 3, 'All pages must expose the same two favicon sizes and Apple icon');
  for (const size of [32, 48, 180]) {
    const icon = links.find(link => link.sizes === `${size}x${size}`);
    assert.ok(icon, `Missing ${size}px icon metadata`);
    const expected = `${basePath}/brand/cangzhi-icon-v1-${size}.png`;
    assert.equal(icon.href, expected, 'Next metadata must not drop or duplicate the gateway prefix');
    assert.equal(icon.rel, size === 180 ? 'apple-touch-icon' : 'icon');
    assert.equal(icon.type, 'image/png');
    const response = await page.request.get(`${origin}${expected}`, { headers: { accept: 'image/png' } });
    assert.equal(response.status(), 200, 'Icons must be reachable before authentication');
    assert.match(response.headers()['content-type'], /^image\/png/);
    assert.deepEqual((await response.body()).subarray(0, 8), Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  }
}

async function verifyPage(page, origin, basePath, route, label, size) {
  await page.goto(`${origin}${basePath}${route}`);
  const mark = page.locator('[data-brand-mark="cangzhi"]');
  await mark.waitFor({ state: 'visible' });
  await until(() => mark.evaluate(element => element.complete && element.naturalWidth > 0), `${label} brand image did not load`);
  assert.equal(await mark.count(), 1, 'Authentication pages must not show a second navbar brand');
  assert.equal(await mark.getAttribute('src'), `${basePath}/brand/cangzhi-icon-v1-96.png`);
  assert.equal(await mark.getAttribute('alt'), '');
  assert.equal(await mark.getAttribute('aria-hidden'), 'true');
  assert.equal(await mark.getAttribute('width'), String(size));
  assert.equal(await mark.getAttribute('height'), String(size));
  assert.equal(await mark.evaluate(element => element.naturalWidth), 96);
  if (phase === 'authenticated') {
    await page.getByRole('button', { name: /品牌验收空间/ }).waitFor();
    const brandLink = page.getByRole('link', { name: '藏知 Cangzhi 首页' });
    assert.equal(await brandLink.getAttribute('href'), `${basePath}/documents`);
    assert.equal(await brandLink.locator('[data-brand-mark="cangzhi"]').count(), 1);
  } else {
    await page.getByRole('heading', { name: phase === 'setup' ? '创建管理员账户' : '欢迎回来', exact: true }).waitFor();
    assert.equal(await page.locator('header').count(), 0);
  }
  await verifyIcons(page, origin, basePath);
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `${label} overflows at ${viewport.width}px`);
    const box = await mark.boundingBox();
    assert.equal(Math.round(box.width), size);
    assert.equal(Math.round(box.height), size);
    assert.ok(box.x >= 0 && box.x + box.width <= viewport.width, `${label} brand is clipped`);
    await page.screenshot({ path: path.join(outputDirectory, `${basePath ? 'gateway' : 'root'}-${label}-${viewport.width}.png`), fullPage: true });
  }
}

async function captureSmallSizes(page, basePath) {
  // A separate contact sheet exercises browser downsampling to a 16px tab icon.
  // It is inserted only into this synthetic test page, not into application UI.
  await page.evaluate(sources => {
    const strip = document.createElement('div');
    strip.id = 'brand-size-proof';
    strip.style.cssText = 'position:fixed;top:0;left:0;z-index:99999;display:flex;align-items:end;gap:24px;padding:24px;background:white;color:#0f172a;font:12px sans-serif';
    for (const size of [16, 32, 48, 96]) {
      const tile = document.createElement('div');
      tile.style.cssText = 'display:grid;justify-items:center;gap:12px';
      const image = document.createElement('img');
      image.src = size <= 32 ? sources.favicon : size === 48 ? sources.medium : sources.mark;
      image.width = size;
      image.height = size;
      image.alt = `${size}px`;
      const label = document.createElement('span');
      label.textContent = `${size}px`;
      tile.append(image, label);
      strip.append(tile);
    }
    document.body.append(strip);
  }, {
    favicon: `${basePath}/brand/cangzhi-icon-v1-32.png`,
    medium: `${basePath}/brand/cangzhi-icon-v1-48.png`,
    mark: `${basePath}/brand/cangzhi-icon-v1-96.png`,
  });
  const strip = page.locator('#brand-size-proof');
  await until(() => strip.locator('img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0)), 'Small-size preview failed to load');
  await strip.screenshot({ path: path.join(outputDirectory, `${basePath ? 'gateway' : 'root'}-icon-sizes.png`) });
  await strip.evaluate(element => element.remove());
}

async function runBuild(basePath, apiPort, browser) {
  const reservation = http.createServer(), port = await listen(reservation);
  await new Promise(resolve => reservation.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.execPath, [require.resolve('next/dist/bin/next'), 'dev', '--hostname', '127.0.0.1', '--port', String(port)], {
    cwd: webRoot,
    env: { ...process.env, API_URL: `http://127.0.0.1:${apiPort}`, NEXT_TELEMETRY_DISABLED: '1', CANGZHI_WEB_BASE_PATH: basePath },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '';
  server.stdout.on('data', data => { log = (log + data).slice(-16000); });
  server.stderr.on('data', data => { log = (log + data).slice(-16000); });
  const exited = new Promise(resolve => server.once('exit', resolve));
  let page;
  try {
    await until(async () => { try { return (await fetch(`${origin}${basePath}/api/auth/status`)).ok; } catch { return false; } }, 'Isolated Next server did not start');
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [], externalRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      if (new URL(route.request().url()).origin === origin) return route.continue();
      externalRequests.push(route.request().url());
      return route.abort();
    });
    phase = 'login';
    await verifyPage(page, origin, basePath, '/login', 'login', 44);
    await captureSmallSizes(page, basePath);
    phase = 'setup';
    await verifyPage(page, origin, basePath, '/setup', 'setup', 44);
    phase = 'authenticated';
    await verifyPage(page, origin, basePath, '/settings/uploads', 'navbar', 36);
    assert.deepEqual(errors, [], 'No client runtime errors');
    assert.deepEqual(externalRequests, [], 'Brand assets must work offline without third-party requests');
    console.log(`PASS brand browser: ${basePath || '/'} login/setup/navbar, local images/metadata, 390/1440 layouts`);
  } catch (error) {
    process.stderr.write(log);
    throw error;
  } finally {
    await page?.close();
    server.kill('SIGTERM');
    await exited;
  }
}

async function main() {
  const apiPort = await listen(fixture);
  let browser;
  try {
    browser = await chromium.launch({ headless: true, executablePath: ['/usr/bin/google-chrome', '/usr/bin/chromium'].find(file => fs.existsSync(file)) });
    await runBuild('', apiPort, browser);
    await runBuild('/_cangzhi', apiPort, browser);
    assert.ok(requests.length > 0);
    assert.ok(requests.every(request => request.method === 'GET'), 'Visual checks must not create accounts or change settings');
    console.log(`Brand screenshots: ${outputDirectory}`);
  } finally {
    await browser?.close();
    await new Promise(resolve => fixture.close(resolve));
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
