/* eslint-disable @typescript-eslint/no-require-imports -- Isolated execution of the real TypeScript UI. */
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
  const evaluatedModule = { exports: {} };
  vm.runInNewContext(code, { module: evaluatedModule, exports: evaluatedModule.exports, require: id => Object.hasOwn(mocks, id) ? mocks[id] : require(id), URLSearchParams, AbortController, DOMException, TextDecoder, ...globals }, { filename });
  return evaluatedModule.exports;
}
const helpers = load('lib/ask-search-state.ts');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const json = body => ({ ok: true, json: async () => body });
function nodes(element) {
  if (!element || typeof element !== 'object') return [];
  return [element, ...[element.props?.children].flat(Infinity).flatMap(nodes)];
}
function text(element) {
  if (element == null) return '';
  if (typeof element !== 'object') return String(element);
  return [element.props?.children].flat(Infinity).map(text).join('');
}
const facets = { categories: [{ id: 1, slug: 'one', name: 'One', document_count: 3 }], tags: [], source_types: [{ value: 'note', label: '笔记' }], connectors: [] };
const answer = value => ({ question: value, answer: value, citations: [], evidence: [], provider: 'mock', model: 'mock', insufficient_evidence: false });
function harness(route, fetcher, params = '', storage = new Map()) {
  const slots = [], pending = [], requests = [], timers = new Map(), guards = [];
  let cursor = 0, dirty = false, tree, timerId = 0;
  const current = { params: new URLSearchParams(params), href: `/${route}${params ? `?${params}` : ''}`, confirm: true };
  const location = { pathname: `/${route}`, search: params ? `?${params}` : '' };
  const hooks = {
    Suspense: () => null,
    useState(initial) {
      const key = cursor++;
      if (!(key in slots)) slots[key] = typeof initial === 'function' ? initial() : initial;
      return [slots[key], value => { const next = typeof value === 'function' ? value(slots[key]) : value; if (!Object.is(slots[key], next)) { slots[key] = next; dirty = true; } }];
    },
    useRef(initial) { const key = cursor++; return slots[key] ??= { current: initial }; },
    useEffect(fn, deps) {
      const key = cursor++, previous = slots[key];
      if (!previous || deps.some((dep, index) => !Object.is(dep, previous.deps[index]))) {
        slots[key] = { deps, cleanup: previous?.cleanup };
        pending.push(() => { slots[key].cleanup?.(); slots[key].cleanup = fn(); });
      }
    },
  };
  const component = load(`app/${route}/page.tsx`, {
    react: hooks, 'next/link': 'a', 'next/navigation': { useSearchParams: () => current.params },
    'react-markdown': 'markdown', 'remark-breaks': () => {}, 'remark-gfm': () => {},
    '@/components/evidence-drawer': { EvidenceDrawer: 'drawer' }, '@/lib/ask-search-state': helpers,
    '@/components/knowledge-preview': { KnowledgePreview: 'knowledge-preview' },
    '@/lib/paths': { withBasePath: value => value },
    '@/lib/navigation-guard': { useUnsavedChanges: (...args) => guards.push(args) },
  }, {
    fetch: (url, init = {}) => { requests.push({ url, ...init }); return fetcher(url, init); },
    HTMLElement: class HTMLElement {},
    document: { cookie: 'cangzhi_workspace=test-space', activeElement: null },
    window: {
      location,
      history: { replaceState: (_state, _title, href) => { current.href = href; location.search = href.includes('?') ? `?${href.split('?')[1]}` : ''; } },
      sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
      setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
      requestAnimationFrame: fn => fn(),
      confirm: () => current.confirm,
    },
  }).default().props.children.type;
  const render = () => { cursor = 0; dirty = false; tree = component(); for (const effect of pending.splice(0)) effect(); };
  const flush = async () => {
    for (let i = 0; i < 50; i++) { await Promise.resolve(); if (dirty || !tree) render(); for (const [id, fn] of [...timers]) { timers.delete(id); fn(); } }
  };
  const button = label => nodes(tree).find(node => node.type === 'button' && text(node) === label);
  return {
    flush, requests, current, storage, guards,
    get tree() { return tree; }, button,
    type: value => nodes(tree).find(node => node.type === (route === 'ask' ? 'textarea' : 'input')).props.onChange({ target: { value } }),
    submit: () => nodes(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }),
    click: label => { assert.ok(button(label), `Missing button ${label}`); button(label).props.onClick(); },
    navigate: async params => { current.params = new URLSearchParams(params); location.search = params ? `?${params}` : ''; current.href = location.pathname + location.search; dirty = true; await flush(); },
  };
}
function askFetcher(custom) {
  return async (url, init) => {
    const handled = custom(url, init);
    if (handled) return handled;
    if (url === '/api/ask/conversations') return json(init.method === 'POST' ? { id: 9 } : { items: [] });
    if (url === '/api/v1/knowledge/scopes') return json({ items: [{ slug: 'all', name: '全部知识' }] });
    if (url === '/api/v1/knowledge/facets') return json(facets);
    if (url === '/api/ask/status') return json({ provider_configured: true });
    if (url === '/api/datasets/summary') return json({ dataset_count: 0, document_count: 0, ready_count: 0, examples: [] });
    throw new Error(`Unexpected request ${url}`);
  };
}

test('preferences are workspace-scoped and discard invalid selections without exposing other spaces', () => {
  assert.notEqual(helpers.askPreferencesKey('cangzhi_workspace=one'), helpers.askPreferencesKey('cangzhi_workspace=two'));
  assert.equal(helpers.askPreferencesKey('other=x'), 'cangzhi:ask-preferences:v3:default');
  const result = helpers.validateAskPreferences({ scopeSlug: 'deleted', categoryIds: [1, 7, '1'], tagIds: [8], sourceTypes: ['note', 'deleted'], mode: 'deep' }, { ...facets, scopes: [{ slug: 'all' }] });
  assert.equal(result.filtersRemoved, true);
  assert.equal(result.preferences.scopeSlug, 'all');
  assert.equal(JSON.stringify(result.preferences.categoryIds), '[1]');
  assert.equal(JSON.stringify(result.preferences.sourceTypes), '["note"]');
});
test('search URL round-trips filters and page, and anchors the primary evidence', () => {
  const url = helpers.searchLocationHref({ query: '关键词', categorySlugs: ['a', 'b'], tagSlugs: ['x'], sourceTypes: ['file'], page: 3 });
  const state = helpers.readSearchLocation(new URLSearchParams(url.split('?')[1]));
  assert.equal(state.page, 3); assert.equal(state.categorySlugs.join(','), 'a,b'); assert.equal(state.query, '关键词');
  assert.equal(helpers.readSearchLocation(new URLSearchParams('page=-4')).page, 1);
  assert.equal(helpers.searchEvidenceHref(7, { id: 8, page: 2 }), '/documents/7?chunk_id=8&page=2');
});
test('real ask initialization restores only this workspace and sends validated filters', async () => {
  const storage = new Map([
    ['cangzhi:ask-preferences:v3:other-space', JSON.stringify({ question: 'foreign draft' })],
    ['cangzhi:ask-preferences:v3:test-space', JSON.stringify({ question: 'local draft', scopeSlug: 'removed', categoryIds: [1, 99], tagIds: [98] })],
  ]);
  const h = harness('ask', askFetcher(url => url === '/api/v1/knowledge/ask' ? Promise.resolve(json(answer('ok'))) : null), '', storage);
  await h.flush();
  assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, 'local draft');
  assert.match(text(h.tree), /已移除当前空间中失效/);
  h.submit(); await h.flush();
  const body = JSON.parse(h.requests.find(request => request.url === '/api/v1/knowledge/ask').body);
  assert.equal(body.scope_slug, 'all'); assert.deepEqual(body.category_ids, [1]); assert.deepEqual(body.tag_ids, []);
});
test('workspace draft restores before recent history, so history cannot silently replace a new draft context', async () => {
  const storage = new Map([['cangzhi:ask-preferences:v3:test-space', JSON.stringify({ question: 'saved new draft' })]]);
  const h = harness('ask', askFetcher((url, init) => url === '/api/ask/conversations' && !init.method ? Promise.resolve(json({ items: [{ id: 1, title: 'old', turn_count: 1 }] })) : null), '', storage);
  await h.flush();
  assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, 'saved new draft');
  assert.equal(h.requests.some(request => request.url === '/api/ask/conversations/1'), false);
});
test('slow facets disable preference controls while allowing a draft, then enable deliberate mode selection', async () => {
  const catalog = deferred();
  const storage = new Map([['cangzhi:ask-preferences:v3:test-space', JSON.stringify({ question: 'saved draft', mode: 'quick' })]]);
  const h = harness('ask', askFetcher(url => url === '/api/v1/knowledge/facets' ? catalog.promise : null), '', storage);
  await h.flush();
  for (const label of ['深度分析', '快速问答', '快速', '细化范围', '知识范围与筛选']) assert.equal(h.button(label).props.disabled, true, label);
  assert.equal(nodes(h.tree).find(node => node.type === 'select' && node.props['aria-label'] === '知识范围').props.disabled, true);
  assert.match(text(h.tree), /正在加载当前空间/);
  h.type('typed while loading'); await h.flush();
  catalog.resolve(json(facets)); await h.flush();
  assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, 'typed while loading');
  assert.equal(h.button('深度分析').props.disabled, false);
  h.click('深度分析'); await h.flush();
  assert.ok(h.button('深度')); assert.equal(text(h.tree).includes('正在加载当前空间'), false);
});
test('new conversation ignores late answer and cleanup, preserving a second in-flight request', async () => {
  const first = deferred(), second = deferred(); let count = 0;
  const h = harness('ask', askFetcher(url => url === '/api/v1/knowledge/ask' ? (++count === 1 ? first.promise : second.promise) : null));
  await h.flush(); h.type('first'); await h.flush(); h.submit(); await h.flush();
  h.click('＋ 新建'); await h.flush(); h.type('second'); await h.flush(); h.submit(); await h.flush();
  first.resolve(json(answer('old answer'))); await h.flush();
  assert.ok(h.button('停止')); assert.equal(text(h.tree).includes('old answer'), false);
  assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, 'second');
  second.resolve(json(answer('new answer'))); await h.flush();
  const turns = nodes(h.tree).filter(node => node.props.turn);
  assert.equal(turns.length, 1); assert.equal(turns[0].props.turn.response.answer, 'new answer');
  assert.equal(h.requests.some(request => request.method === 'DELETE'), false);
});
test('late request error cannot clear a newer request or show its error in the new view', async () => {
  const old = deferred(), next = deferred(); let count = 0;
  const h = harness('ask', askFetcher(url => url === '/api/v1/knowledge/ask' ? (++count === 1 ? old.promise : next.promise) : null));
  await h.flush(); h.type('one'); await h.flush(); h.submit(); await h.flush(); h.click('停止'); await h.flush();
  h.type('two'); await h.flush(); h.submit(); await h.flush(); old.reject(new Error('old failure')); await h.flush();
  assert.ok(h.button('停止')); assert.equal(text(h.tree).includes('old failure'), false);
  next.resolve(json(answer('done'))); await h.flush();
});
test('a response does not erase input edited while it was running', async () => {
  const pending = deferred();
  const h = harness('ask', askFetcher(url => url === '/api/v1/knowledge/ask' ? pending.promise : null));
  await h.flush(); h.type('submitted'); await h.flush(); h.submit(); await h.flush(); h.type('new draft'); await h.flush();
  pending.resolve(json(answer('done'))); await h.flush();
  assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, 'new draft');
});
test('slow conversation creation after new cannot start an unwanted model request', async () => {
  const created = deferred();
  const h = harness('ask', askFetcher((url, init) => url === '/api/ask/conversations' && init.method === 'POST' ? created.promise : null));
  await h.flush(); h.type('old'); await h.flush(); h.submit(); await h.flush(); h.click('＋ 新建'); await h.flush();
  created.resolve(json({ id: 90 })); await h.flush();
  assert.equal(h.requests.some(request => request.url === '/api/v1/knowledge/ask'), false);
  assert.equal(h.requests.some(request => request.method === 'DELETE'), false);
});
test('history load races and new discard stale content and errors', async () => {
  const a = deferred(), b = deferred();
  const h = harness('ask', askFetcher((url, init) => {
    if (url === '/api/ask/conversations' && !init.method) return json({ items: [{ id: 1, title: 'A', turn_count: 1 }, { id: 2, title: 'B', turn_count: 1 }] });
    if (url === '/api/ask/conversations/1') return a.promise;
    if (url === '/api/ask/conversations/2') return b.promise;
    return null;
  }), 'q=draft');
  await h.flush(); h.click('A1 条问答'); await h.flush(); h.click('B1 条问答'); await h.flush();
  b.resolve(json({ id: 2, turns: [{ id: 2, question: 'B', mode: 'quick', response: answer('B') }] })); await h.flush();
  a.reject(new Error('A failed')); await h.flush();
  assert.equal(nodes(h.tree).find(node => node.props.turn)?.props.turn.question, 'B');
  assert.equal(text(h.tree).includes('A failed'), false);
});
test('new conversation cancels slow history restore and preserves the new draft', async () => {
  const history = deferred();
  const h = harness('ask', askFetcher((url, init) => {
    if (url === '/api/ask/conversations' && !init.method) return json({ items: [{ id: 1, title: 'A', turn_count: 1 }] });
    if (url === '/api/ask/conversations/1') return history.promise;
    return null;
  }));
  await h.flush(); h.click('＋ 新建'); await h.flush(); h.type('keep draft'); await h.flush();
  history.resolve(json({ id: 1, turns: [{ id: 1, question: 'old', mode: 'quick', response: answer('old') }] })); await h.flush();
  assert.equal(nodes(h.tree).filter(node => node.props.turn).length, 0);
  assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, 'keep draft');
});
test('initial history list arriving after new populates the list but never opens the old conversation', async () => {
  const list = deferred();
  const h = harness('ask', askFetcher((url, init) => url === '/api/ask/conversations' && !init.method ? list.promise : null));
  await h.flush(); h.click('＋ 新建'); await h.flush(); h.type('new draft'); await h.flush();
  list.resolve(json({ items: [{ id: 1, title: 'A', turn_count: 1 }] })); await h.flush();
  assert.ok(h.button('A1 条问答'));
  assert.equal(h.requests.some(request => request.url === '/api/ask/conversations/1'), false);
  assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, 'new draft');
});
test('late deep progress and result cannot enter a replacement stream', async () => {
  let firstController, secondController, calls = 0;
  const first = new ReadableStream({ start(controller) { firstController = controller; } });
  const second = new ReadableStream({ start(controller) { secondController = controller; } });
  const h = harness('ask', askFetcher(url => url === '/api/v1/knowledge/ask/stream' ? Promise.resolve({ ok: true, body: ++calls === 1 ? first : second }) : null));
  await h.flush(); h.click('深度分析'); await h.flush(); h.type('old deep'); await h.flush(); h.submit(); await h.flush();
  h.click('＋ 新建'); await h.flush(); h.type('new deep'); await h.flush(); h.submit(); await h.flush();
  const emit = (controller, event) => controller.enqueue(new TextEncoder().encode(JSON.stringify(event) + '\n'));
  emit(firstController, { type: 'progress', message: 'stale progress' });
  emit(secondController, { type: 'progress', message: 'current progress' }); await h.flush();
  const analysis = nodes(h.tree).find(node => node.props.analysis)?.props.analysis;
  assert.equal(analysis.question, 'new deep'); assert.equal(analysis.message, 'current progress');
  emit(firstController, { type: 'result', data: answer('stale result') }); firstController.close(); await h.flush();
  assert.ok(h.button('停止')); assert.equal(nodes(h.tree).filter(node => node.props.turn).length, 0);
  emit(secondController, { type: 'result', data: answer('fresh result') }); secondController.close(); await h.flush();
  assert.equal(nodes(h.tree).find(node => node.props.turn).props.turn.response.answer, 'fresh result');
});
test('search restores page/filter URL, sends correct offsets and resets page on refinement', async () => {
  const h = harness('search', async (url, init) => {
    if (url === '/api/search/filters') return json(facets);
    const body = JSON.parse(init.body);
    return json({ query: body.query, total: 100, limit: 20, offset: body.offset, hits: [], retrieval: { mode: 'filters', vector_used: false } });
  }, 'category=one&source=note&page=3');
  await h.flush();
  assert.equal(JSON.parse(h.requests.find(request => request.url === '/api/search').body).offset, 40);
  assert.match(h.current.href, /category=one/); assert.match(h.current.href, /page=3/);
  h.click('上一页'); await h.flush(); assert.equal(JSON.parse(h.requests.at(-1).body).offset, 20);
  await h.navigate('category=one&source=note&page=4');
  assert.equal(JSON.parse(h.requests.at(-1).body).offset, 60); assert.match(h.current.href, /page=4/);
  // Filter chips are child components; exercise the passed real page callback.
  nodes(h.tree).find(node => node.props.onToggleCategory).props.onToggleCategory('one'); await h.flush();
  assert.equal(JSON.parse(h.requests.at(-1).body).offset, 0); assert.equal(h.current.href.includes('category='), false);
});
test('an old search cannot replace newer results, even when the transport ignores abort', async () => {
  const first = deferred(), second = deferred(); let count = 0;
  const h = harness('search', async url => url === '/api/search/filters' ? json(facets) : (++count === 1 ? first.promise : second.promise), 'q=first');
  await h.flush(); h.type('second'); await h.flush();
  const payload = query => ({ query, total: 1, limit: 20, offset: 0, hits: [{ document_id: 1, chunk: { id: 1 } }] });
  second.resolve(json(payload('second'))); await h.flush();
  first.resolve(json(payload('first'))); await h.flush();
  assert.equal(nodes(h.tree).find(node => node.props.payload)?.props.payload.query, 'second');
  assert.equal(h.current.href, '/search?q=second');
});
test('single-document asks isolate and restore their own draft after refresh/login without changing the workspace draft', async () => {
  const normalKey = helpers.askPreferencesKey('cangzhi_workspace=test-space');
  const documentKey = helpers.askPreferencesKey('cangzhi_workspace=test-space', [20]);
  const storage = new Map([[normalKey, JSON.stringify({ question: 'workspace draft', categoryIds: [1] })]]);
  const fetcher = askFetcher(url => url === '/api/v1/knowledge/ask' ? Promise.resolve(json(answer('restricted answer'))) : null);
  const h = harness('ask', fetcher, 'document_ids=20', storage); await h.flush();
  assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, '');
  h.type('document-specific draft'); await h.flush();
  assert.equal(JSON.parse(storage.get(documentKey)).question, 'document-specific draft');
  assert.equal(JSON.parse(storage.get(normalKey)).question, 'workspace draft');
  const returned = harness('ask', fetcher, 'document_ids=20', storage); await returned.flush();
  assert.equal(nodes(returned.tree).find(node => node.type === 'textarea').props.value, 'document-specific draft');
  returned.submit(); await returned.flush();
  const request = JSON.parse(returned.requests.find(item => item.url === '/api/v1/knowledge/ask').body);
  assert.deepEqual(request.document_ids, [20]); assert.deepEqual(request.category_ids, []);
  assert.equal(JSON.parse(storage.get(normalKey)).question, 'workspace draft');
  assert.equal(helpers.askPreferencesKey('cangzhi_workspace=test-space', [21, 20, 21]), helpers.askPreferencesKey('cangzhi_workspace=test-space', [20, 21]));
});
test('invalid document selection never silently sends an unrestricted ask', async () => {
  const h = harness('ask', askFetcher(() => null), 'document_ids=bad&q=question'); await h.flush();
  assert.equal(h.button('发送').props.disabled, true); h.submit(); await h.flush();
  assert.equal(h.requests.some(item => item.url === '/api/v1/knowledge/ask'), false);
});
test('workbench area changes and evidence collapse preserve input and selected citation without dialogs', async () => {
  const h = harness('ask', askFetcher(url => url === '/api/v1/knowledge/ask' ? Promise.resolve(json(answer('answer'))) : null));
  await h.flush(); h.type('question'); await h.flush(); h.submit(); await h.flush(); h.type('draft to preserve'); await h.flush();
  const selected = { id: 1, document_id: 20, document_version_id: 21, chunk_id: 22, title: 'Synthetic', heading_path: [], page: null, paragraph_index: null, source_type: 'note', source_url: null, snippet: 'Evidence' };
  nodes(h.tree).find(node => node.props.turn).props.onOpenCitation(selected); await h.flush();
  assert.equal(nodes(h.tree).find(node => node.props.id === 'ask-evidence').props.className.startsWith('flex'), true);
  const drawer = nodes(h.tree).find(node => node.type === 'drawer'); assert.equal(drawer.props.citation, selected);
  drawer.props.onClose(); await h.flush();
  assert.equal(nodes(h.tree).find(node => node.type === 'drawer').props.citation, selected);
  h.click('范围与记录'); await h.flush(); h.click('问答'); await h.flush();
  assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, 'draft to preserve');
  assert.equal(nodes(h.tree).some(node => node.props.role === 'dialog' || node.props['aria-modal']), false);
  h.click('收起侧栏'); await h.flush(); assert.ok(h.button('范围与记录'));
});
test('new conversation protects a draft when confirmation is declined', async () => {
  const h = harness('ask', askFetcher(() => null)); await h.flush(); h.type('keep me'); await h.flush(); h.current.confirm = false;
  h.click('＋ 新建'); await h.flush(); assert.equal(nodes(h.tree).find(node => node.type === 'textarea').props.value, 'keep me');
});
test('storage failure is visible and enables leave protection instead of silently losing the draft', async () => {
  const storage = new Map(); storage.set = () => { throw new Error('storage unavailable'); };
  const h = harness('ask', askFetcher(() => null), '', storage); await h.flush(); h.type('unsaved'); await h.flush();
  assert.match(text(h.tree), /浏览器无法保存草稿/); assert.equal(h.guards.at(-1)[0], true);
  h.click('清空草稿'); await h.flush(); assert.equal(h.guards.at(-1)[0], false);
});
