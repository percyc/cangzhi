/* eslint-disable @typescript-eslint/no-require-imports -- Node CommonJS test harness evaluates transpiled UI modules. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

// Evaluate pure UI modules and form handlers without a browser, credentials or API.
function load(relative, mocks = {}) {
  const filename = path.join(__dirname, '..', relative);
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText;
  const evaluatedModule = { exports: {} };
  const localRequire = (id) => Object.hasOwn(mocks, id) ? mocks[id] : require(id);
  vm.runInThisContext(`(function(require,module,exports){${code}\n})`, { filename })(localRequire, evaluatedModule, evaluatedModule.exports);
  return evaluatedModule.exports;
}
const ux = load('lib/usability.ts');

test('all app page routes have guidance, except authentication and root redirects', () => {
  function pages(dir) {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => entry.isDirectory()
      ? pages(path.join(dir, entry.name)) : entry.name === 'page.tsx' ? [path.join(dir, entry.name)] : []);
  }
  const root = path.join(__dirname, '../app');
  for (const file of pages(root)) {
    const route = '/' + path.relative(root, path.dirname(file)).replaceAll(path.sep, '/').replaceAll('[id]', '21');
    if (['/', '/login', '/setup'].includes(route)) assert.equal(ux.pageHelp(route), null);
    else assert.ok(ux.pageHelp(route), `Missing guide: ${route}`);
  }
});
test('settings grouping has nine unique valid routes and guides', () => {
  const items = ux.SETTINGS_GROUPS.flatMap(group => group.items);
  assert.equal(items.length, 9);
  assert.equal(new Set(items.map(item => item.id)).size, 9);
  for (const item of items) {
    const url = new URL(item.href, 'https://example.test');
    const help = ux.pageHelp(url.pathname, url.searchParams.get('section'));
    assert.ok(help);
    assert.equal(help.steps.length, 3);
  }
});
test('guidance handles aliases, invalid model panels and excludes unknown paths', () => {
  assert.equal(ux.pageHelp('/processing'), ux.pageHelp('/inbox'));
  assert.equal(ux.pageHelp('/sources'), ux.pageHelp('/settings/sources'));
  assert.equal(ux.pageHelp('/settings', 'invalid'), ux.pageHelp('/settings', 'chat'));
  assert.notEqual(ux.pageHelp('/settings', 'ocr'), ux.pageHelp('/settings', 'chat'));
  assert.equal(ux.pageHelp('/not-a-page'), null);
  assert.equal(ux.pageHelp('/documents/21/extra'), null);
});
test('navigation highlights related knowledge pages and legacy aliases without prefix collisions', () => {
  for (const route of ['/notes/new', '/notes/21/edit', '/documents/21', '/categories', '/tags', '/files/upload', '/links/new']) assert.ok(ux.activeNavigation(route, '/documents'));
  assert.ok(ux.activeNavigation('/processing', '/inbox'));
  assert.ok(ux.activeNavigation('/sources', '/settings'));
  assert.ok(ux.activeNavigation('/settings/account', '/settings'));
  assert.equal(ux.activeNavigation('/documents-extra', '/documents'), false);
});
test('category empty state never implies the whole library is empty', () => {
  assert.equal(ux.libraryEmptyState(false, true).kind, 'filtered');
  assert.equal(ux.libraryEmptyState(false, false).kind, 'new');
  assert.equal(ux.libraryEmptyState(true, true).kind, 'trash');
});
test('Chinese workspace names receive a valid automatic identifier', () => {
  assert.equal(ux.workspaceSlug('研究资料', 'space-demo123'), 'space-demo123');
  assert.equal(ux.workspaceSlug('  Research_Project  ', 'space-demo123'), 'research-project');
  assert.equal(ux.workspaceSlug('   ', 'space-demo123'), '');
  assert.equal(ux.workspaceSlug('A'.repeat(100), 'space-demo123').length, 64);
  for (const name of ['中文', 'A B', '!Research!', '中文 2026']) assert.match(ux.workspaceSlug(name, 'space-demo123'), /^[a-z0-9][a-z0-9-]*$/);
});
test('API errors are readable strings and never echo validation input objects', () => {
  assert.equal(ux.apiErrorMessage({ detail: '无权限' }, '失败'), '无权限');
  assert.equal(ux.apiErrorMessage({ detail: { message: '连接失败' } }, '失败'), '连接失败');
  assert.equal(ux.apiErrorMessage({ detail: [{ msg: 'invalid', input: 'private' }] }, '失败'), '失败');
  for (const body of [null, 3, {}, { detail: {} }]) assert.equal(ux.apiErrorMessage(body, '失败'), '失败');
});

function descendants(element) {
  if (!element || typeof element !== 'object') return [];
  const children = element.props?.children;
  return [element, ...[children].flat(Infinity).flatMap(descendants)];
}
function noteForm(values) {
  let index = 0;
  const state = [...values];
  const pushes = [];
  const component = load('app/notes/new/page.tsx', {
    react: {
      useState: (initial) => { const key = index++; state[key] ??= initial; return [state[key], value => { state[key] = value; }]; },
      useRef: (initial) => ({ current: initial }),
      useEffect: (callback) => callback(),
    },
    'next/navigation': { useRouter: () => ({ push: url => pushes.push(url) }) },
    'next/link': () => null,
    '@/lib/usability': ux,
    '@/components/note-draft-status': { NoteDraftStatus: () => null },
    '@/lib/navigation-guard': { useUnsavedChanges() {}, runWithoutNavigationGuard: (callback) => callback() },
    '@/lib/note-drafts': {
      browserDraftStorage: () => ({}), clearNoteDraft: () => true,
      noteWorkspace: () => 'default', noteDraftKey: () => 'synthetic-key',
      readNoteDraft: () => ({ draft: null, status: 'empty' }), writeNoteDraft: () => 'saved',
    },
  });
  const originalDocument = global.document;
  global.document = { cookie: '' };
  let form;
  try { form = descendants(component.default()).find(element => element.type === 'form'); }
  finally { global.document = originalDocument; }
  return { submit: () => form.props.onSubmit({ preventDefault() {} }), state, pushes };
}
test('blank note is rejected without an API request', async () => {
  const form = noteForm(['', '   ', false, '']);
  await form.submit();
  assert.equal(form.state[2], false);
  assert.match(form.state[3], /正文/);
});
test('busy note ignores duplicate submits', async () => {
  const form = noteForm(['test', 'content', true, '']);
  await form.submit();
  assert.deepEqual(form.pushes, []);
  assert.equal(form.state[2], true);
});
test('network failure unlocks save and preserves the note for retry', async () => {
  const original = global.fetch;
  try {
    global.fetch = async () => { throw new TypeError('network'); };
    const form = noteForm(['test', 'synthetic content', false, '']);
    await form.submit();
    assert.equal(form.state[1], 'synthetic content');
    assert.equal(form.state[2], false);
    assert.match(form.state[3], /正文仍保留/);
  } finally { global.fetch = original; }
});
test('HTTP object errors do not crash rendering or leave save pending', async () => {
  const original = global.fetch;
  try {
    global.fetch = async () => ({ ok: false, json: async () => ({ detail: { message: '请重新登录' } }) });
    const form = noteForm(['test', 'content', false, '']);
    await form.submit();
    assert.equal(form.state[2], false);
    assert.equal(form.state[3], '请重新登录');
  } finally { global.fetch = original; }
});
test('successful note returns to detail and retains untitled-note behavior', async () => {
  const original = global.fetch;
  let submitted;
  try {
    global.fetch = async (url, init) => { submitted = { url, body: JSON.parse(init.body) }; return { ok: true, json: async () => ({ id: 21 }) }; };
    const form = noteForm(['', 'content', false, '']);
    await form.submit();
    assert.equal(submitted.url, '/api/notes');
    assert.equal(submitted.body.generate_title, true);
    assert.deepEqual(form.pushes, ['/documents/21']);
  } finally { global.fetch = original; }
});
test('responsive settings selector honors unsaved-change guard before navigation', () => {
  const pushed = [], selected = [];
  const component = load('components/SettingsSectionNav.tsx', {
    'next/navigation': { useRouter: () => ({ push: href => pushed.push(href) }) },
    'next/link': () => null, '@/lib/usability': ux,
  });
  const blocked = component.SettingsSectionNav({ active: 'chat', beforeNavigate: () => false, onSelect: id => selected.push(id) });
  descendants(blocked).find(element => element.type === 'select').props.onChange({ target: { value: 'account' } });
  assert.deepEqual(pushed, []); assert.deepEqual(selected, []);
  const allowed = component.SettingsSectionNav({ active: 'chat', beforeNavigate: () => true, onSelect: id => selected.push(id) });
  descendants(allowed).find(element => element.type === 'select').props.onChange({ target: { value: 'ocr' } });
  assert.deepEqual(pushed, ['/settings?section=ocr']); assert.deepEqual(selected, ['ocr']);
});
