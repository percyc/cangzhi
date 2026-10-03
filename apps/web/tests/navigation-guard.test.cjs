/* eslint-disable @typescript-eslint/no-require-imports -- Test actual transpiled browser helpers without credentials. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, globals = {}, mocks = {}) {
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const mod = { exports: {} };
  vm.runInNewContext(code, { module: mod, exports: mod.exports, require: id => mocks[id] || require(id), URL, queueMicrotask, ...globals });
  return mod.exports;
}

function surface() {
  const windowEvents = {}, documentEvents = {}, moves = [];
  const window = {
    location: new URL('https://demo.test/notes/new'),
    confirm: () => false,
    history: {
      state: { next: 'preserve' },
      pushState(state) { this.state = state; },
      replaceState(state) { this.state = state; },
      go(delta) { moves.push(delta); },
    },
    addEventListener(name, fn) { windowEvents[name] = fn; },
    removeEventListener(name) { delete windowEvents[name]; },
  };
  const document = {
    addEventListener(name, fn) { documentEvents[name] = fn; },
    removeEventListener(name) { delete documentEvents[name]; },
  };
  class Element {
    constructor(href, target = '', download = false) { this.href = href; this.target = target; this.download = download; }
    closest() { return this; }
    hasAttribute(name) { return name === 'download' && this.download; }
  }
  const lib = load('lib/navigation-guard.ts', { window, document, Element }, { react: { useEffect() {} } });
  const cleanup = lib.installNavigationGuards();
  function event(props = {}) { return { prevented: false, stopped: false, button: 0, ...props, preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } }; }
  return { lib, cleanup, window, windowEvents, documentEvents, Element, event, moves };
}

test('dirty page blocks click and unload; registration removal cleans up', () => {
  const s = surface();
  const release = s.lib.registerUnsavedChanges('未保存');
  const event = s.event({ target: new s.Element('/documents') });
  s.documentEvents.click(event);
  assert.equal(event.prevented, true); assert.equal(event.stopped, true);
  const unload = s.event(); s.windowEvents.beforeunload(unload);
  assert.equal(unload.returnValue, ''); assert.equal(unload.prevented, true);
  release();
  assert.equal(s.lib.confirmNavigation(), true);
  s.cleanup();
  assert.deepEqual(Object.keys(s.windowEvents), []);
});

test('new tabs, downloads and same-page navigation do not discard the editor', () => {
  const s = surface(); s.lib.registerUnsavedChanges('未保存');
  for (const props of [
    { target: new s.Element('/documents', '_blank') },
    { target: new s.Element('/api/file', '', true) },
    { target: new s.Element('/documents'), ctrlKey: true },
    { target: new s.Element('/notes/new#content') },
  ]) {
    const event = s.event(props); s.documentEvents.click(event); assert.equal(event.prevented, false);
  }
  s.cleanup();
});

test('approved navigation bypass lasts for this event only, including nesting', async () => {
  const s = surface(); s.lib.registerUnsavedChanges('未保存');
  s.lib.runWithoutNavigationGuard(() => {
    s.lib.runWithoutNavigationGuard(() => assert.equal(s.lib.confirmNavigation(), true));
  });
  assert.equal(s.lib.confirmNavigation(), true);
  await Promise.resolve();
  assert.equal(s.lib.confirmNavigation(), false);
  s.cleanup();
});

test('cancelled browser back restores the indexed position without losing Next state', () => {
  const s = surface();
  const first = s.window.history.state;
  assert.equal(first.next, 'preserve');
  s.window.history.pushState({ tree: 'next-router' }, '', '/notes/new');
  const second = s.window.history.state;
  assert.equal(second.tree, 'next-router');
  s.window.history.replaceState({ tree: 'retained' }, '', '/notes/new?tab=edit');
  assert.equal(s.window.history.state.__cangzhiNavigationPosition, second.__cangzhiNavigationPosition);
  s.lib.registerUnsavedChanges('未保存');
  const back = s.event({ state: first }); s.windowEvents.popstate(back);
  assert.equal(back.stopped, true); assert.deepEqual(s.moves, [1]);
  const restore = s.event({ state: second }); s.windowEvents.popstate(restore);
  assert.equal(restore.stopped, true);
  s.window.confirm = () => true;
  const accepted = s.event({ state: first }); s.windowEvents.popstate(accepted);
  assert.equal(accepted.stopped, false);
  s.cleanup();
});

test('cancelled browser forward moves backward rather than pushing a duplicate entry', () => {
  const s = surface(), first = s.window.history.state;
  s.window.history.pushState({}, '', '/documents'); const second = s.window.history.state;
  s.windowEvents.popstate(s.event({ state: first }));
  s.lib.registerUnsavedChanges('未保存');
  s.windowEvents.popstate(s.event({ state: second }));
  assert.deepEqual(s.moves, [-1]);
  s.cleanup();
});

test('login return accepts local routes and rejects executable/external and loop destinations', () => {
  const { safeLoginReturn } = load('lib/login-return.ts', {}, { './paths': { WEB_BASE_PATH: '' } });
  assert.equal(safeLoginReturn('/notes/21/edit?from=search#content'), '/notes/21/edit?from=search#content');
  for (const value of ['javascript:alert(1)', '//outside.test', '/\\outside.test', '/%5coutside.test', '/%2foutside.test', '/login/', '/setup', '/api/auth/logout', '/%0afile', undefined]) {
    assert.equal(safeLoginReturn(value), '/documents', String(value));
  }
});

test('gateway return strips base path once, never sends the router outside the app', () => {
  const { safeLoginReturn } = load('lib/login-return.ts', {}, { './paths': { WEB_BASE_PATH: '/cangzhi' } });
  assert.equal(safeLoginReturn('/cangzhi/notes/new'), '/notes/new');
  assert.equal(safeLoginReturn('/cangzhi/login'), '/documents');
  assert.equal(safeLoginReturn('/documents'), '/documents');
});
