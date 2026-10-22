/* eslint-disable @typescript-eslint/no-require-imports -- Exercise real local assets and isolated TS modules without a browser. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const sharp = require('sharp');

const webRoot = path.join(__dirname, '..');
const sizes = [32, 48, 96, 180, 512];

function load(relative, mocks = {}, basePath = '') {
  const code = ts.transpileModule(fs.readFileSync(path.join(webRoot, relative), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  const testModule = { exports: {} };
  vm.runInThisContext(`(function(require,module,exports,process){${code}\n})`)(
    id => Object.hasOwn(mocks, id) ? mocks[id] : require(id), testModule, testModule.exports,
    { env: { NEXT_PUBLIC_CANGZHI_WEB_BASE_PATH: basePath } },
  );
  return testModule.exports;
}

function modules(basePath = '') {
  const paths = load('lib/paths.ts', {}, basePath);
  const brand = load('lib/brand.ts', { '@/lib/paths': paths });
  return { paths, brand };
}

test('brand PNG exports are square, transparent, nonempty and bounded for their display size', async () => {
  const byteBudgets = { 32: 12000, 48: 18000, 96: 40000, 180: 100000, 512: 300000 };
  for (const size of sizes) {
    const file = path.join(webRoot, `public/brand/cangzhi-icon-v1-${size}.png`);
    const buffer = fs.readFileSync(file);
    assert.deepEqual(buffer.subarray(0, 8), Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
    assert.ok(buffer.length <= byteBudgets[size], `${size}px asset is unexpectedly large: ${buffer.length} bytes`);
    const metadata = await sharp(buffer).metadata();
    assert.equal(metadata.format, 'png');
    assert.equal(metadata.width, size);
    assert.equal(metadata.height, size);
    assert.equal(metadata.hasAlpha, true, `${size}px export must preserve transparent corners`);
    const alpha = (await sharp(buffer).ensureAlpha().stats()).channels.at(-1);
    assert.equal(alpha.min, 0, `${size}px export must not become an opaque square`);
    assert.equal(alpha.max, 255, `${size}px export must not be blank or translucent throughout`);
  }
});

for (const configuredBasePath of ['', '/_cangzhi', ' /_cangzhi/ ']) {
  test(`brand image paths and icon metadata are local and prefixed exactly once (${configuredBasePath || 'root'})`, () => {
    const { paths, brand } = modules(configuredBasePath);
    const prefix = configuredBasePath ? '/_cangzhi' : '';
    for (const size of sizes) {
      const expected = `${prefix}/brand/cangzhi-icon-v1-${size}.png`;
      assert.equal(brand.brandIconPath(size), expected);
      assert.equal(paths.withBasePath(brand.brandIconPath(size)), expected, 'Repeated path normalization must be idempotent');
    }
    assert.deepEqual(brand.BRAND_ICONS, {
      icon: [
        { url: `${prefix}/brand/cangzhi-icon-v1-32.png`, sizes: '32x32', type: 'image/png' },
        { url: `${prefix}/brand/cangzhi-icon-v1-48.png`, sizes: '48x48', type: 'image/png' },
      ],
      apple: [{ url: `${prefix}/brand/cangzhi-icon-v1-180.png`, sizes: '180x180', type: 'image/png' }],
    });
  });
}

test('shared brand mark reserves layout space and avoids duplicate accessible names and optimizer requests', () => {
  for (const basePath of ['', '/_cangzhi']) {
    const { brand } = modules(basePath);
    const Image = () => null;
    const { BrandMark } = load('components/BrandMark.tsx', { 'next/image': Image, '@/lib/brand': brand });
    for (const size of [undefined, 44]) {
      const element = BrandMark({ size, className: 'fixture-brand' });
      assert.equal(element.type, Image);
      assert.equal(element.props.src, `${basePath}/brand/cangzhi-icon-v1-96.png`);
      assert.equal(element.props.alt, '');
      assert.equal(element.props['aria-hidden'], 'true');
      assert.equal(element.props['data-brand-mark'], 'cangzhi');
      assert.equal(element.props.width, size || 36);
      assert.equal(element.props.height, size || 36);
      assert.equal(element.props.unoptimized, true, 'Local brand assets must not depend on an image optimizer');
      assert.equal(element.props.loading, 'eager');
      assert.match(element.props.className, /shrink-0/);
      assert.match(element.props.className, /fixture-brand/);
    }
  }
});

test('application metadata uses the shared favicon definitions in both root and gateway builds', () => {
  for (const basePath of ['', '/_cangzhi']) {
    const { paths, brand } = modules(basePath);
    const { metadata } = load('app/layout.tsx', {
      '@/lib/paths': paths, '@/lib/brand': brand, './globals.css': {},
      '@/components/TopNav': { TopNav: () => null },
      '@/components/PageGuide': { PageGuide: () => null },
      '@/lib/navigation-guard': { NavigationGuard: () => null },
    });
    assert.equal(metadata.icons, brand.BRAND_ICONS);
    assert.equal(metadata.applicationName, 'Cangzhi');
    assert.match(metadata.title.default, /藏知/);
  }
});
