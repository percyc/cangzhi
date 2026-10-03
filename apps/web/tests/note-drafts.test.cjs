/* eslint-disable @typescript-eslint/no-require-imports -- Isolated Node tests execute real transpiled note pages. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(relative, mocks = {}, globals = {}) {
  const filename = path.join(__dirname, '..', relative);
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true,
  } }).outputText;
  const evaluated = { exports: {} };
  vm.runInNewContext(code, { module: evaluated, exports: evaluated.exports, require: (id) => Object.hasOwn(mocks, id) ? mocks[id] : require(id), TextEncoder, Date, console, ...globals }, { filename });
  return evaluated.exports;
}
function storage() {
  const values = new Map();
  return { values, getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
}
const drafts = load('lib/note-drafts.ts');
const plain = (value) => JSON.parse(JSON.stringify(value));
const example = { title: '合成标题', content: '合成测试正文', baseVersion: null };
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const originalNote = (version = 10, title = '已保存标题', content = '已保存正文') => ({ source_type: 'note', title, current_version: { id: version, raw_content: content } });
function deferred() { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; }
function nodes(element) {
  if (!element || typeof element !== 'object') return [];
  if (typeof element.type === 'function') return nodes(element.type(element.props));
  return [element, ...[element.props?.children].flat(Infinity).flatMap(nodes)];
}
function text(element) {
  if (typeof element === 'string' || typeof element === 'number') return String(element);
  if (!element || typeof element !== 'object') return '';
  if (typeof element.type === 'function') return text(element.type(element.props));
  return [element.props?.children].flat(Infinity).map(text).join('');
}
const find = (tree, type, label) => nodes(tree).find((node) => node.type === type && (label === undefined || text(node).includes(label)));
function pageHarness({ edit = false, id = '21', store = storage(), cookie = '', fetcher = async () => response(originalNote()) } = {}) {
  const slots = [], effects = [], pushes = [], timers = new Map();
  let index = 0, dirty = true, tree, mounted = true, writesAfterUnmount = 0, guarded = false, bypasses = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, position) => Object.is(value, b[position]));
  const react = {
    useState(initial) {
      const key = index++;
      slots[key] ??= { value: typeof initial === 'function' ? initial() : initial };
      return [slots[key].value, (next) => {
        if (!mounted) writesAfterUnmount++;
        const value = typeof next === 'function' ? next(slots[key].value) : next;
        if (!Object.is(slots[key].value, value)) { slots[key].value = value; dirty = true; }
      }];
    },
    useRef(initial) { const key = index++; slots[key] ??= { current: initial }; return slots[key]; },
    useEffect(callback, deps) {
      const key = index++;
      if (!same(slots[key]?.deps, deps)) effects.push(() => { slots[key]?.cleanup?.(); slots[key] = { deps, cleanup: callback() }; });
    },
  };
  const window = {
    sessionStorage: store, confirm: () => true,
    setTimeout: (callback) => { const id = Symbol('timer'); timers.set(id, callback); return id; },
    clearTimeout: (id) => timers.delete(id),
  };
  const document = { cookie };
  const library = load('lib/note-drafts.ts', {}, { window });
  const mocks = {
    react, 'next/link': 'a', 'next/navigation': { useRouter: () => ({ push: (url) => pushes.push(url) }), useParams: () => ({ id }) },
    '@/lib/usability': load('lib/usability.ts'), '@/lib/note-drafts': library,
    '@/lib/navigation-guard': { useUnsavedChanges: (enabled) => { guarded = enabled; }, runWithoutNavigationGuard: (callback) => { bypasses++; return callback(); } },
    '@/components/note-draft-status': load('components/note-draft-status.tsx', { '@/lib/note-drafts': library }),
  };
  const component = load(edit ? 'app/notes/[id]/edit/page.tsx' : 'app/notes/new/page.tsx', mocks, { window, document, fetch: (url, init) => fetcher(url, init), AbortController });
  function render() {
    index = 0; dirty = false; tree = component.default();
    while (effects.length) effects.shift()();
    return tree;
  }
  const page = {
    store, pushes, window, document,
    get tree() { while (dirty) render(); return tree; },
    get guarded() { void page.tree; return guarded; },
    get bypasses() { return bypasses; },
    get writesAfterUnmount() { return writesAfterUnmount; },
    expireDraft() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach((callback) => callback()); },
    async settle() { for (let i = 0; i < 30; i++) { if (dirty) render(); await Promise.resolve(); } },
    change(type, value) { find(page.tree, type).props.onChange({ target: { value } }); },
    submit() { return find(page.tree, 'form').props.onSubmit({ preventDefault() {} }); },
    setId(value) { id = value; dirty = true; },
    unmount() { slots.forEach((slot) => slot?.cleanup?.()); mounted = false; },
  };
  return page;
}

test('draft keys isolate workspace, new notes and each document', () => {
  const keys = ['default', 'research'].flatMap((space) => ['new', '21', '22'].map((id) => drafts.noteDraftKey(space, id)));
  assert.equal(new Set(keys).size, 6);
  assert.equal(drafts.noteWorkspace('x=1; cangzhi_workspace=research; y=2'), 'research');
  for (const cookie of ['', 'cangzhi_workspace=%broken', 'cangzhi_workspace=../invalid']) assert.equal(drafts.noteWorkspace(cookie), 'default');
});
test('serialization permits only note fields and metadata, never extra form credentials', () => {
  const store = storage();
  assert.equal(drafts.writeNoteDraft(store, 'key', { ...example, apiKey: 'must-not-persist', password: 'must-not-persist', cookie: 'must-not-persist' }, 100), 'saved');
  const serialized = JSON.parse(store.getItem('key'));
  assert.deepEqual(Object.keys(serialized).sort(), ['draft', 'savedAt', 'schema']);
  assert.deepEqual(serialized.draft, example);
  assert.ok(!store.getItem('key').includes('must-not-persist'));
  assert.deepEqual(plain(drafts.readNoteDraft(store, 'key', 101).draft), example);
});
test('TTL expires after 24 hours and cleanup touches only the selected draft', () => {
  const store = storage();
  drafts.writeNoteDraft(store, 'key', example, 100);
  store.setItem('unrelated', 'keep');
  assert.equal(drafts.readNoteDraft(store, 'key', 100 + drafts.NOTE_DRAFT_TTL_MS - 1).status, 'restored');
  assert.equal(drafts.readNoteDraft(store, 'key', 100 + drafts.NOTE_DRAFT_TTL_MS).status, 'expired');
  assert.equal(store.getItem('key'), null);
  assert.equal(store.getItem('unrelated'), 'keep');
});
test('invalid or future-dated drafts are rejected without rendering arbitrary objects', () => {
  const store = storage();
  for (const value of ['broken json', '{"schema":1}', JSON.stringify({ schema: 1, savedAt: 999999, draft: example }), JSON.stringify({ schema: 1, savedAt: 100, draft: { ...example, title: {} } })]) {
    store.setItem('key', value);
    assert.equal(drafts.readNoteDraft(store, 'key', 100).status, 'invalid');
    assert.equal(store.getItem('key'), null);
  }
});
test('UTF-8 size limit rejects oversized drafts while retaining the last safe copy', () => {
  const store = storage();
  drafts.writeNoteDraft(store, 'key', example);
  const before = store.getItem('key');
  assert.equal(drafts.writeNoteDraft(store, 'key', { ...example, content: '中'.repeat(drafts.NOTE_DRAFT_MAX_BYTES / 3) }), 'too-large');
  assert.equal(store.getItem('key'), before);
});
test('blocked browser storage degrades explicitly without throwing', () => {
  const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('quota'); }, removeItem() { throw new Error('blocked'); } };
  for (const store of [null, blocked]) {
    assert.equal(drafts.readNoteDraft(store, 'key').status, 'unavailable');
    assert.equal(drafts.writeNoteDraft(store, 'key', example), 'unavailable');
    assert.equal(drafts.clearNoteDraft(store, 'key'), false);
  }
});
test('note decoding requires an actual note and readable version', () => {
  assert.deepEqual(plain(drafts.noteSnapshot(originalNote())), { title: '已保存标题', content: '已保存正文', baseVersion: 10 });
  for (const value of [null, {}, { source_type: 'file' }, { ...originalNote(), current_version: null }, originalNote(0)]) assert.throws(() => drafts.noteSnapshot(value));
});

test('new-note text persists synchronously, restores after remount and can be discarded', async () => {
  const store = storage(), key = drafts.noteDraftKey('research', 'new');
  const page = pageHarness({ store, cookie: 'cangzhi_workspace=research' });
  await page.settle(); page.change('input', '草稿标题'); page.change('textarea', '草稿正文');
  assert.equal(page.guarded, false);
  assert.equal(JSON.parse(store.getItem(key)).draft.content, '草稿正文');
  page.unmount();
  const restored = pageHarness({ store, cookie: 'cangzhi_workspace=research' });
  await restored.settle();
  assert.equal(find(restored.tree, 'textarea').props.value, '草稿正文');
  assert.match(text(restored.tree), /已恢复本标签页/);
  find(restored.tree, 'button', '丢弃草稿').props.onClick();
  assert.equal(find(restored.tree, 'textarea').props.value, '');
  assert.equal(store.getItem(key), null);
});
test('new-note restoration does not leak into a different workspace', async () => {
  const store = storage();
  drafts.writeNoteDraft(store, drafts.noteDraftKey('research', 'new'), example);
  const page = pageHarness({ store, cookie: 'cangzhi_workspace=default' });
  await page.settle();
  assert.equal(find(page.tree, 'textarea').props.value, '');
  assert.equal(store.values.size, 1);
});
test('an open page no longer treats an expired local draft as safe to leave', async () => {
  const page = pageHarness();
  await page.settle(); page.change('textarea', '未正式保存的正文'); await page.settle();
  assert.equal(page.guarded, false);
  page.expireDraft();
  assert.equal(page.guarded, true);
  assert.match(text(page.tree), /暂存草稿已超过 24 小时/);
  page.change('textarea', '继续写作并重新暂存');
  assert.equal(page.guarded, false);
});
test('storage failure protects dirty inputs, while successfully saved local drafts need no leave prompt', async () => {
  const store = storage(); store.setItem = () => { throw new Error('quota'); };
  const page = pageHarness({ store });
  await page.settle();
  assert.equal(page.guarded, false);
  page.change('textarea', '未能暂存的正文');
  assert.equal(page.guarded, true);
  assert.match(text(page.tree), /浏览器暂存不可用/);
});
test('401 retains the new note draft and provides a safe login-return path', async () => {
  const page = pageHarness({ fetcher: async () => response({}, 401) });
  await page.settle(); page.change('textarea', '登录过期前的草稿');
  await page.submit();
  assert.match(text(page.tree), /登录已过期/);
  assert.equal(find(page.tree, 'a', '重新登录后继续').props.href, '/login?next=%2Fnotes%2Fnew');
  assert.equal(drafts.readNoteDraft(page.store, drafts.noteDraftKey('default', 'new')).draft.content, '登录过期前的草稿');
  assert.equal(page.pushes.length, 0);
});
test('successful new-note save clears only its draft and bypasses the in-flight guard', async () => {
  const store = storage(), pending = deferred();
  store.setItem('unrelated', 'keep');
  const page = pageHarness({ store, fetcher: () => pending.promise });
  await page.settle(); page.change('textarea', '将要保存的正文');
  const save = page.submit();
  assert.equal(page.guarded, true);
  pending.resolve(response({ id: 88 })); await save;
  assert.equal(store.getItem(drafts.noteDraftKey('default', 'new')), null);
  assert.equal(store.getItem('unrelated'), 'keep');
  assert.deepEqual(page.pushes, ['/documents/88']);
  assert.equal(page.bypasses, 1);
});
test('server success with storage-cleanup failure is not offered as another save', async () => {
  const store = storage(), page = pageHarness({ store, fetcher: async () => response({ id: 88 }) });
  await page.settle(); page.change('textarea', '已保存正文');
  store.removeItem = () => { throw new Error('blocked'); };
  await page.submit();
  assert.match(text(page.tree), /笔记已保存到知识库/);
  assert.equal(find(page.tree, 'button', '保存').props.disabled, true);
  assert.equal(page.guarded, false);
});
test('failed original load has no editable save form and can retry successfully', async () => {
  let success = false;
  const page = pageHarness({ edit: true, fetcher: async () => success ? response(originalNote()) : response({}, 500) });
  await page.settle();
  assert.equal(find(page.tree, 'form'), undefined);
  assert.match(text(page.tree), /无法读取/);
  success = true; find(page.tree, 'button', '重新读取').props.onClick(); await page.settle();
  assert.equal(find(page.tree, 'textarea').props.value, '已保存正文');
  assert.ok(find(page.tree, 'form'));
});
test('edit load 401 preserves recoverable text as read-only and returns to that note after login', async () => {
  const store = storage();
  drafts.writeNoteDraft(store, drafts.noteDraftKey('default', '21'), { ...example, baseVersion: 10 });
  const page = pageHarness({ edit: true, store, fetcher: async () => response({}, 401) });
  await page.settle();
  assert.equal(find(page.tree, 'form'), undefined);
  assert.equal(find(page.tree, 'textarea').props.readOnly, true);
  assert.match(find(page.tree, 'textarea').props.value, /合成测试正文/);
  assert.equal(find(page.tree, 'a', '重新登录后继续').props.href, '/login?next=%2Fnotes%2F21%2Fedit');
});
test('old-version draft is never silently rebased or submitted', async () => {
  const store = storage(), calls = [];
  drafts.writeNoteDraft(store, drafts.noteDraftKey('default', '21'), { ...example, baseVersion: 9 });
  const page = pageHarness({ edit: true, store, fetcher: async (url, init) => { calls.push({ url, init }); return response(originalNote(10)); } });
  await page.settle();
  assert.match(text(page.tree), /已保存的原文有变化/);
  assert.equal(find(page.tree, 'textarea').props.value, example.content);
  assert.equal(find(page.tree, 'button', '保存修改').props.disabled, true);
  await page.submit();
  assert.equal(calls.length, 1);
  find(page.tree, 'button', '已核对，继续使用草稿').props.onClick();
  assert.equal(find(page.tree, 'button', '保存修改').props.disabled, false);
  assert.equal(drafts.readNoteDraft(store, drafts.noteDraftKey('default', '21')).draft.baseVersion, 10);
});
test('edit save rechecks original version and blocks a newer server version before PATCH', async () => {
  let version = 10;
  const calls = [], page = pageHarness({ edit: true, fetcher: async (url, init) => { calls.push({ url, init }); return response(originalNote(version)); } });
  await page.settle(); page.change('textarea', '保留的修改'); version = 11;
  await page.submit();
  assert.equal(calls.some((call) => call.init.method === 'PATCH'), false);
  assert.equal(find(page.tree, 'textarea').props.value, '保留的修改');
  assert.match(text(page.tree), /其他页面更新/);
});
test('edit save handles 401 without dropping draft, and successful retry cleans it', async () => {
  let expired = false;
  const calls = [], page = pageHarness({ edit: true, cookie: 'cangzhi_workspace=research', fetcher: async (url, init) => {
    calls.push({ url, init }); return expired ? response({}, 401) : response(originalNote());
  } });
  await page.settle(); page.change('textarea', '修改草稿'); expired = true;
  await page.submit();
  assert.match(text(page.tree), /登录已过期/);
  assert.ok(drafts.readNoteDraft(page.store, drafts.noteDraftKey('research', '21')).draft);
  expired = false; await page.submit();
  assert.equal(page.store.getItem(drafts.noteDraftKey('research', '21')), null);
  const patch = calls.find((call) => call.init.method === 'PATCH');
  assert.equal(patch.init.headers['X-Cangzhi-Workspace'], 'research');
  assert.deepEqual(JSON.parse(patch.init.body), { title: '已保存标题', content: '修改草稿' });
  assert.deepEqual(page.pushes, ['/documents/21']);
});
test('stale document fetch cannot replace another note after route switch', async () => {
  const old = deferred();
  const page = pageHarness({ edit: true, fetcher: async (url) => url.endsWith('/21') ? old.promise : response(originalNote(22, '资料二', '第二份正文')) });
  await page.settle(); page.setId('22'); await page.settle();
  old.resolve(response(originalNote(21, '资料一', '迟到正文'))); await page.settle();
  assert.equal(find(page.tree, 'textarea').props.value, '第二份正文');
});
test('a previous note save cannot unlock or clear the next note save', async () => {
  const pending = new Map([['21', deferred()], ['22', deferred()]]), patches = [];
  const page = pageHarness({ edit: true, fetcher: async (url, init) => {
    const id = url.split('/').at(-1);
    if (init.method === 'PATCH') { patches.push(id); return pending.get(id).promise; }
    return response(originalNote(Number(id), `资料${id}`, `正文${id}`));
  } });
  await page.settle(); page.change('textarea', '资料21修改');
  const first = page.submit(); await page.settle();
  page.setId('22'); await page.settle(); page.change('textarea', '资料22修改');
  const second = page.submit(); await page.settle();
  pending.get('21').resolve(response({ id: 21 })); await first;
  assert.equal(page.guarded, true);
  assert.ok(page.store.getItem(drafts.noteDraftKey('default', '22')));
  await page.submit();
  assert.deepEqual(patches, ['21', '22']);
  pending.get('22').resolve(response({ id: 22 })); await second;
  assert.deepEqual(page.pushes, ['/documents/22']);
});
test('successful in-flight save after unmount clears its captured draft without updating another page', async () => {
  const pending = deferred(), page = pageHarness({ fetcher: () => pending.promise });
  await page.settle(); page.change('textarea', '已提交的正文');
  const save = page.submit(); page.unmount(); pending.resolve(response({ id: 99 })); await save;
  assert.equal(page.store.getItem(drafts.noteDraftKey('default', 'new')), null);
  assert.equal(page.writesAfterUnmount, 0);
  assert.deepEqual(page.pushes, []);
});
