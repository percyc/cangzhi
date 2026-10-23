/* eslint-disable @typescript-eslint/no-require-imports -- Evaluate real UI modules with synthetic hooks and no network. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(relative, mocks = {}) {
  const filename = path.join(__dirname, '..', relative);
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText;
  const evaluatedModule = { exports: {} };
  vm.runInThisContext(`(function(require,module,exports){${code}\n})`, { filename })(
    id => Object.hasOwn(mocks, id) ? mocks[id] : require(id), evaluatedModule, evaluatedModule.exports);
  return evaluatedModule.exports;
}
const ux = load('lib/usability.ts');
const search = load('lib/settings-search.ts', { './usability': ux });

test('every settings section and main product workflow has a local search entry', () => {
  const ids = search.SETTINGS_SEARCH_ENTRIES.map(entry => entry.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const item of ux.SETTINGS_GROUPS.flatMap(group => group.items)) {
    const entry = search.SETTINGS_SEARCH_ENTRIES.find(entry => entry.section === item.id);
    assert.ok(entry, item.id);
    assert.equal(entry.href, item.href);
  }
  for (const id of ['documents', 'upload-file', 'new-note', 'new-link', 'search', 'ask', 'inbox', 'processing', 'categories', 'tags', 'trash']) assert.ok(ids.includes(id), id);
  for (const entry of search.SETTINGS_SEARCH_ENTRIES) {
    assert.match(entry.href, /^\/(settings|documents|files|notes|links|search|ask|inbox|processing|categories|tags)([/?]|$)/);
    const url = new URL(entry.href, 'https://synthetic.test');
    assert.ok(ux.pageHelp(url.pathname, url.searchParams.get('section')), entry.href);
  }
});

test('Chinese common requests, vocabulary and case-normalized terms find the intended page first', () => {
  for (const [query, first] of [
    ['修改密码', 'account'], ['请问在哪里修改密码？', 'account'], ['上传上限', 'uploads'],
    ['文件太大怎么办', 'uploads'], ['上传太大', 'uploads'], ['怎么调上传大小', 'uploads'],
    ['添加数据库', 'sources'], ['我想添加数据库', 'sources'],
    ['模型', 'chat'], ['第一次使用', 'setup'], ['ＯＰＥＮＡＩ', 'chat'],
    ['WebDAV', 'sources'], ['webdev', 'sources'], ['扫描PDF', 'ocr'], ['Embedding', 'embedding'],
    ['模型并发', 'overview'], ['模型怎么配置', 'setup'], ['磁盘容量', 'overview'], ['磁盘占用', 'overview'],
    ['MCP', 'access'], ['关键词', 'search'],
    ['数据集统计', 'ask'], ['上传文件', 'upload-file'], ['重新解析', 'documents'], ['回收站', 'trash'],
  ]) assert.equal(search.searchSettings(query)[0]?.id, first, query);
});

test('search supports spaced terms, returns bounded deterministic results and genuine empty states', () => {
  assert.equal(search.searchSettings('模型 地址')[0]?.id, 'chat');
  for (const query of ['', '   ', '???', '不存在的功能xyz123']) assert.deepEqual(search.searchSettings(query), [], query);
  assert.equal(search.searchSettings('模型', 2).length, 2);
  assert.equal(search.searchSettings('模型', 0).length, 0);
  assert.equal(search.searchSettings('模型', -1).length, 0);
  assert.ok(search.searchSettings('模型').length <= 8);
  assert.deepEqual(search.searchSettings('模型'), search.searchSettings('模型'));
  assert.deepEqual(search.searchSettings('x'.repeat(120) + '修改密码'), []);
});

function descendants(element) {
  if (!element || typeof element !== 'object') return [];
  return [element, ...[element.props?.children].flat(Infinity).flatMap(descendants)];
}
function renderSearch(query = '', beforeNavigate) {
  const state = [query], refs = [];
  const Link = () => null;
  const component = load('components/SettingsSearch.tsx', {
    'next/link': Link, '@/lib/settings-search': search,
    react: { useState: () => [state[0], value => { state[0] = value; }], useId: () => 'synthetic-search',
      useRef: initial => { const ref = { current: initial }; refs.push(ref); return ref; } },
  });
  const tree = component.default({ beforeNavigate });
  const all = descendants(tree);
  return { state, refs, tree, all, input: all.find(node => node.type === 'input'), links: all.filter(node => node.type === Link) };
}
function event(key) { return { key, nativeEvent: { isComposing: false }, button: 0, prevented: false, preventDefault() { this.prevented = true; } }; }

test('search has accessible input, example shortcuts and explicit privacy guidance', () => {
  const ui = renderSearch();
  assert.equal(ui.input.props.type, 'search');
  assert.equal(ui.input.props.autoComplete, 'off');
  assert.equal(ui.input.props.maxLength, 120);
  assert.ok(ui.all.some(node => node.type === 'label' && node.props.htmlFor === ui.input.props.id));
  assert.ok(ui.all.some(node => node.props.id === ui.input.props['aria-describedby']));
  assert.equal(ui.links.length, 0);
  const example = ui.all.find(node => node.type === 'button' && node.props.children === '修改密码');
  example.props.onClick();
  assert.equal(ui.state[0], '修改密码');
});

test('empty search is explained and clearing restores an empty in-memory query', () => {
  const ui = renderSearch('不存在的功能xyz123');
  assert.equal(ui.links.length, 0);
  assert.ok(ui.all.some(node => node.props.role === 'status' && node.props.children === '没有匹配的入口'));
  assert.ok(ui.all.some(node => typeof node.props.children === 'string' && node.props.children.includes('若要查文档内容')));
  ui.all.find(node => node.type === 'button' && node.props.children === '清除').props.onClick();
  assert.equal(ui.state[0], '');
});

test('search result navigation honors the existing guard and does not guard a new-tab click', () => {
  let called = 0;
  const ui = renderSearch('修改密码', entry => { called++; assert.equal(entry.section, 'account'); return false; });
  const blocked = event('');
  ui.links[0].props.onClick(blocked);
  assert.equal(blocked.prevented, true); assert.equal(called, 1);
  const newTab = { ...event(''), ctrlKey: true };
  ui.links[0].props.onClick(newTab);
  assert.equal(newTab.prevented, false); assert.equal(called, 1);
  assert.equal(ui.links[0].props.href, '/settings/account');
  assert.equal(ui.links[0].props.prefetch, false);
});

test('keyboard supports arrow focus, Enter guarded native click, Escape and IME composition', () => {
  const ui = renderSearch('模型');
  let focused = -1, clicks = 0;
  ui.refs[0].current = { focus: () => { focused = -2; } };
  ui.refs[1].current = ui.links.map((_, index) => ({ focus: () => { focused = index; }, click: () => { clicks++; } }));
  ui.input.props.onKeyDown(event('ArrowDown')); assert.equal(focused, 0);
  ui.links[0].props.onKeyDown(event('ArrowDown')); assert.equal(focused, 1);
  ui.links[1].props.onKeyDown(event('ArrowUp')); assert.equal(focused, 0);
  ui.links[0].props.onKeyDown(event('ArrowUp')); assert.equal(focused, -2);
  ui.input.props.onKeyDown(event('Enter')); assert.equal(clicks, 1);
  ui.input.props.onKeyDown({ ...event('Enter'), nativeEvent: { isComposing: true } }); assert.equal(clicks, 1);
  ui.links[0].props.onKeyDown(event('Escape')); assert.equal(ui.state[0], ''); assert.equal(focused, -2);
});

test('settings search delegates section changes and protects non-settings navigation', () => {
  let allowed = false, globalAllowed = false;
  const selected = [];
  const Search = () => null;
  const component = load('components/SettingsSectionNav.tsx', {
    react: { useState: () => [false, () => {}], useId: () => 'synthetic-navigation' },
    'next/link': () => null, 'next/navigation': { useRouter: () => ({ push() {} }) },
    '@/lib/usability': ux, '@/components/VersionInfo': () => null, '@/components/SettingsSearch': Search,
    '@/lib/navigation-guard': { confirmNavigation: () => globalAllowed },
  });
  const tree = component.SettingsSectionNav({ active: 'chat', beforeNavigate: () => allowed, onSelect: section => selected.push(section) });
  const navigate = descendants(tree).find(node => node.type === Search).props.beforeNavigate;
  assert.equal(navigate({ section: 'ocr' }), false); assert.deepEqual(selected, []);
  allowed = true;
  assert.equal(navigate({ section: 'ocr' }), true); assert.deepEqual(selected, ['ocr']);
  assert.equal(navigate({ id: 'documents' }), false);
  globalAllowed = true;
  assert.equal(navigate({ id: 'documents' }), true);
});

test('mobile settings selector uses the global guard when a page supplies no section-specific guard', () => {
  let allowed = false;
  const pushed = [];
  const component = load('components/SettingsSectionNav.tsx', {
    react: { useState: () => [false, () => {}], useId: () => 'synthetic-navigation' },
    'next/link': () => null, 'next/navigation': { useRouter: () => ({ push: href => pushed.push(href) }) },
    '@/lib/usability': ux, '@/components/VersionInfo': () => null, '@/components/SettingsSearch': () => null,
    '@/lib/navigation-guard': { confirmNavigation: () => allowed },
  });
  const tree = component.SettingsSectionNav({ active: 'uploads' });
  const select = descendants(tree).find(node => node.type === 'select');
  select.props.onChange({ target: { value: 'account' } });
  assert.deepEqual(pushed, []);
  allowed = true;
  select.props.onChange({ target: { value: 'account' } });
  assert.deepEqual(pushed, ['/settings/account']);
});

test('mobile settings tools are collapsed by default, accessible, and remain mounted when toggled', () => {
  let expanded = false;
  const Search = () => null;
  const component = load('components/SettingsSectionNav.tsx', {
    react: { useState: () => [expanded, value => { expanded = value; }], useId: () => 'synthetic-navigation' },
    'next/link': () => null, 'next/navigation': { useRouter: () => ({ push() {} }) },
    '@/lib/usability': ux, '@/components/VersionInfo': () => null, '@/components/SettingsSearch': Search,
    '@/lib/navigation-guard': { confirmNavigation: () => true },
  });
  for (const expected of [false, true, false]) {
    const nodes = descendants(component.SettingsSectionNav({ active: 'setup' }));
    const toggle = nodes.find(node => node.type === 'button' && node.props['aria-controls']);
    assert.equal(toggle.props['aria-expanded'], expected);
    const panel = nodes.find(node => node.props.id === toggle.props['aria-controls']);
    assert.ok(panel); assert.match(panel.props.className, /lg:block/);
    assert.equal(panel.props.className.includes('hidden'), !expected);
    assert.equal(nodes.filter(node => node.type === Search).length, 1, 'Search stays mounted to preserve query');
    toggle.props.onClick();
  }
});

test('current section is a no-op but modified clicks retain native link behavior', () => {
  const selected = [];
  let guards = 0;
  const Search = () => null;
  const component = load('components/SettingsSectionNav.tsx', {
    react: { useState: () => [false, () => {}], useId: () => 'navigation' },
    'next/link': () => null, 'next/navigation': { useRouter: () => ({ push() { throw new Error('Unexpected navigation'); } }) },
    '@/lib/usability': ux, '@/components/VersionInfo': () => null, '@/components/SettingsSearch': Search,
    '@/lib/navigation-guard': { confirmNavigation: () => true },
  });
  const nodes = descendants(component.SettingsSectionNav({ active: 'chat', onSelect: id => selected.push(id), beforeNavigate: () => { guards++; return true; } }));
  const current = nodes.find(node => node.props['aria-current'] === 'page');
  const click = event(); current.props.onClick(click);
  assert.equal(click.prevented, true);
  const modified = { ...event(), ctrlKey: true }; current.props.onClick(modified);
  assert.equal(modified.prevented, false);
  assert.equal(nodes.find(node => node.type === Search).props.beforeNavigate({ section: 'chat' }), false);
  nodes.find(node => node.type === 'select').props.onChange({ target: { value: 'chat' } });
  assert.equal(guards, 0); assert.deepEqual(selected, []);
});

test('persistent layout ignores callbacks belonging to a departed route', () => {
  const Nav = () => null;
  const Context = { Provider: () => null };
  let pathname = '/settings/uploads';
  const staleProps = { active: 'chat', beforeNavigate: () => false };
  const component = load('components/SettingsLayout.tsx', {
    react: { useState: () => [{ pathname: '/settings', props: staleProps }, () => {}] },
    'next/navigation': { usePathname: () => pathname },
    '@/components/SettingsSectionNav': { SettingsSectionNav: Nav },
    '@/components/SettingsNavigationState': { SettingsNavigationContext: Context },
    '@/lib/usability': ux,
  });
  let nav = descendants(component.default({ children: 'content' })).find(node => node.type === Nav);
  assert.equal(nav.props.active, 'uploads'); assert.equal(nav.props.beforeNavigate, undefined);
  pathname = '/settings';
  nav = descendants(component.default({ children: 'content' })).find(node => node.type === Nav);
  assert.equal(nav.props.beforeNavigate, staleProps.beforeNavigate);
});

test('form navigation registration cleans up without erasing a newer owner', () => {
  let value = null, cleanup;
  const publish = next => { value = typeof next === 'function' ? next(value) : next; };
  const component = load('components/SettingsNavigationState.tsx', {
    react: { createContext: () => ({}), useContext: () => publish, useLayoutEffect: effect => { cleanup = effect(); } },
    'next/navigation': { usePathname: () => '/settings' },
  });
  component.SettingsNavigationRegistration({ active: 'ocr' });
  assert.equal(value.props.active, 'ocr');
  const oldCleanup = cleanup;
  component.SettingsNavigationRegistration({ active: 'chat' });
  oldCleanup(); assert.equal(value.props.active, 'chat');
  cleanup(); assert.equal(value, null);
});
