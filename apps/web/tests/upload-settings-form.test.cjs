/* eslint-disable @typescript-eslint/no-require-imports -- Synthetic React handler tests. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const vm = require('node:vm');

function form(value, fetch) {
  const state = [50, value, false, '', ''];
  let index = 0;
  const mocks = {
    react: { useEffect() {}, useState: () => {
      const key = index++;
      return [state[key], next => { state[key] = next; }];
    } },
    'next/link': () => null,
    '@/components/SettingsSectionNav': { SettingsSectionNav: () => null },
    '@/lib/navigation-guard': { useUnsavedChanges() {} },
    '@/lib/paths': { withApiBasePath: value => `/_test${value}` },
  };
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../app/settings/uploads/page.tsx'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  const testModule = { exports: {} };
  vm.runInThisContext(`(function(require,module,exports,fetch){${code}\n})`)(
    id => mocks[id] || require(id), testModule, testModule.exports, fetch);
  const tree = testModule.exports.default();
  const element = tree.props.children.find(child => child && child.type === 'form');
  return { state, submit: () => element.props.onSubmit({ preventDefault() {} }) };
}

test('upload setting validates positive integer before sending', async () => {
  for (const value of ['0', '-1', '', '1.5']) {
    const rendered = form(value, () => assert.fail('no request'));
    await rendered.submit();
    assert.match(rendered.state[4], /正整数/);
    assert.equal(rendered.state[0], 50);
  }
});

test('save uses gateway path and confirms only persisted upload limit', async () => {
  const rendered = form('100', async (url, options) => {
    assert.equal(url, '/_test/api/system/upload-settings');
    assert.equal(options.method, 'PUT');
    assert.equal(JSON.parse(options.body).max_upload_size_mb, 100);
    return { ok: true, json: async () => ({ max_upload_size_mb: 100 }) };
  });
  await rendered.submit();
  assert.equal(rendered.state[0], 100);
  assert.equal(rendered.state[2], false);
  assert.match(rendered.state[3], /立即生效/);
});

test('failed upload setting save preserves draft and previous effective limit', async () => {
  const rendered = form('100', async () => ({ ok: false }));
  await rendered.submit();
  assert.equal(rendered.state[0], 50);
  assert.equal(rendered.state[1], '100');
  assert.equal(rendered.state[2], false);
  assert.match(rendered.state[4], /尚未更改/);
});
