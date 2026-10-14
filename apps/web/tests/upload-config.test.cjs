/* eslint-disable @typescript-eslint/no-require-imports -- Node config tests. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../next.config.js'), 'utf8');
const MiB = 1024 * 1024;

function config(env = {}) {
  const context = { module: { exports: {} }, process: { env } };
  vm.runInNewContext(source, context);
  return context.module.exports;
}

test('default proxy accepts a 50 MiB file plus multipart overhead', () => {
  assert.equal(config().experimental.proxyClientMaxBodySize, 51 * MiB);
  assert.equal(config({ MAX_UPLOAD_SIZE_MB: '' }).experimental.proxyClientMaxBodySize, 51 * MiB);
});
test('custom API limit also controls proxy allowance', () => {
  for (const mb of [1, 10, 100, 200]) {
    assert.equal(config({ MAX_UPLOAD_SIZE_MB: String(mb) }).experimental.proxyClientMaxBodySize, (mb + 1) * MiB);
  }
});
test('invalid limits fail explicitly instead of silently reverting to 10 MiB', () => {
  for (const value of ['0', '-1', 'NaN', 'Infinity', '50mb', '1.5', ' ', '9007199254740991']) {
    assert.throws(() => config({ MAX_UPLOAD_SIZE_MB: value }), /MAX_UPLOAD_SIZE_MB/);
  }
});
test('root and gateway rewrites retain API destination and upload allowance', async () => {
  for (const base of ['', '/_cangzhi']) {
    const resolved = config({ API_URL: 'http://127.0.0.1:18000', CANGZHI_WEB_BASE_PATH: base });
    assert.equal(resolved.basePath, base);
    assert.equal(resolved.experimental.proxyClientMaxBodySize, 51 * MiB);
    const routes = await resolved.rewrites();
    assert.equal(routes[0].destination, 'http://127.0.0.1:18000/api/:path*');
    assert.equal(routes.length, base ? 2 : 1);
  }
});
