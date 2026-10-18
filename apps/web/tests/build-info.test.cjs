/* eslint-disable @typescript-eslint/no-require-imports -- Test actual transpiled version UI using synthetic metadata. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const build = { version: 'v1.3.2', revision: 'a'.repeat(40), built_at: '2026-10-18T00:00:00Z', dirty: false };
function evaluate(relative, mocks = {}, extra = {}) {
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '..', relative), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const testModule = { exports: {} };
  vm.runInNewContext(code, { module: testModule, exports: testModule.exports, require: id => mocks[id] || require(id),
    process: { env: {} }, AbortController, ...extra });
  return testModule.exports;
}
const lib = evaluate('lib/build-info.ts');
const nodes = value => !value || typeof value !== 'object' ? [] : [value, ...[value.props?.children].flat(Infinity).flatMap(nodes)];
const content = value => typeof value === 'string' ? value : !value || typeof value !== 'object' ? '' : [value.props?.children].flat(Infinity).map(content).join('');
function panel(fetcher) {
  const states = [], refs = [], cleanups = []; let index = 0;
  const react = { useState(initial) { const i = index++; if (!(i in states)) states[i] = initial;
    return [states[i], next => { states[i] = next; }]; },
    useRef(initial) { const i = index++; refs[i] ??= { current: initial }; return refs[i]; },
    useEffect(callback) { if (!cleanups.length) cleanups.push(callback()); } };
  const Component = evaluate('components/VersionInfo.tsx', { react, '@/lib/build-info': { ...lib, WEB_BUILD: build },
    '@/lib/paths': { withApiBasePath: value => '/_test' + value } }, { fetch: fetcher }).default;
  return { tree() { index = 0; return Component(); }, close() { cleanups.forEach(fn => fn?.()); } };
}
const settle = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

test('unknown/dirty builds never claim consistency; revisions and release labels both matter', () => {
  assert.equal(lib.WEB_BUILD.version, 'unversioned');
  assert.equal(lib.buildLabel(lib.WEB_BUILD), '未标记构建');
  assert.equal(lib.compareBuilds(build, { ...build }), 'match');
  assert.equal(lib.compareBuilds(build, { ...build, revision: 'b'.repeat(40) }), 'mismatch');
  assert.equal(lib.compareBuilds(build, { ...build, version: 'v1.3.3' }), 'mismatch');
  assert.equal(lib.compareBuilds(build, { ...build, dirty: true }), 'unknown');
  assert.equal(lib.compareBuilds(build, { ...build, revision: 'unknown' }), 'unknown');
});
test('version details load only when opened and use authenticated gateway route without caching', async () => {
  let calls = 0;
  const ui = panel(async (url, options) => {
    calls++; assert.equal(url, '/_test/api/system/version'); assert.equal(options.cache, 'no-store');
    return { ok: true, json: async () => ({ ...build, revision: 'b'.repeat(40) }) };
  });
  const tree = ui.tree(); assert.equal(calls, 0);
  tree.props.onToggle({ currentTarget: { open: true } }); await settle();
  assert.match(content(ui.tree()), /Web\/API版本不一致/);
  ui.tree().props.onToggle({ currentTarget: { open: false } });
  ui.tree().props.onToggle({ currentTarget: { open: true } }); await settle();
  assert.equal(calls, 1); ui.close();
});
test('version request failure is visible, retry clears stale comparison and validates response', async () => {
  let response = { ok: false, status: 401 };
  const ui = panel(async () => response);
  ui.tree().props.onToggle({ currentTarget: { open: true } }); await settle();
  assert.match(content(ui.tree()), /登录已失效/);
  response = { ok: true, json: async () => ({ ...build }) };
  nodes(ui.tree()).find(node => node.type === 'button').props.onClick(); await settle();
  assert.match(content(ui.tree()), /Web\/API版本一致/);
  response = { ok: true, json: async () => ({ version: 'invalid' }) };
  nodes(ui.tree()).find(node => node.type === 'button').props.onClick(); await settle();
  assert.match(content(ui.tree()), /未提供完整/);
  assert.doesNotMatch(content(ui.tree()), /Web\/API版本一致/); ui.close();
});
test('unmount cancels the metadata request', async () => {
  let signal;
  const ui = panel((_url, options) => { signal = options.signal; return new Promise(() => {}); });
  ui.tree().props.onToggle({ currentTarget: { open: true } }); ui.close();
  assert.equal(signal.aborted, true);
});
