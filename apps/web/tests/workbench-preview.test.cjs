/* eslint-disable @typescript-eslint/no-require-imports -- Execute actual TypeScript UI with isolated hooks and HTTP fixtures. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const nodes = (value) => !value || typeof value !== 'object' ? [] : [value, ...[value.props?.children].flat(Infinity).flatMap(nodes)];
const text = (value) => value == null || typeof value === 'boolean' ? '' : typeof value !== 'object' ? String(value) : [value.props?.children].flat(Infinity).map(text).join('');
const find = (tree, type, label) => nodes(tree).find((node) => node.type === type && (label === undefined || text(node).includes(label)));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => value });

function load(relative, mocks = {}, globals = {}, extra = '') {
  const filename = path.join(__dirname, '..', relative);
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8') + extra, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText;
  const evaluatedModule = { exports: {} };
  vm.runInNewContext(code, { module: evaluatedModule, exports: evaluatedModule.exports, require: (id) => Object.hasOwn(mocks, id) ? mocks[id] : require(id), URL, URLSearchParams, AbortController, console, ...globals }, { filename });
  return evaluatedModule.exports;
}
const helpers = load('lib/ask-search-state.ts');

function harness(name, props, fetcher, params = '', basePath = '') {
  const slots = [], pending = [], timers = new Map(), requests = [];
  let index = 0, dirty = true, tree, timerId = 0, mounted = true, postUnmountWrites = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, position) => Object.is(value, b[position]));
  const hooks = {
    Suspense: 'suspense',
    useState(initial) {
      const key = index++;
      if (!slots[key]) slots[key] = { value: typeof initial === 'function' ? initial() : initial };
      return [slots[key].value, (next) => {
        if (!mounted) postUnmountWrites++;
        const value = typeof next === 'function' ? next(slots[key].value) : next;
        if (!Object.is(value, slots[key].value)) { slots[key].value = value; dirty = true; }
      }];
    },
    useRef(initial) { const key = index++; slots[key] ??= { current: initial }; return slots[key]; },
    useEffect(callback, deps) {
      const key = index++;
      if (!same(slots[key]?.deps, deps)) pending.push(() => { slots[key]?.cleanup?.(); slots[key] = { deps, cleanup: callback() }; });
    },
  };
  const current = { params: new URLSearchParams(params), href: `${basePath}/search${params ? `?${params}` : ''}`, scroll: null, historyWrites: [], location: new URL(`https://fixture.test${basePath}/search${params ? `?${params}` : ''}`) };
  const window = {
    location: current.location,
    history: { replaceState: (_state, _title, href) => { current.href = href; current.historyWrites.push(href); current.location.href = new URL(href, current.location).href; } },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; }, clearTimeout(id) { timers.delete(id); },
    scrollY: 640, scrollTo(value) { current.scroll = value.top; }, matchMedia: () => ({ matches: true }),
  };
  const file = name === 'SearchClient' || name === 'Results' ? 'app/search/page.tsx' : 'components/knowledge-preview.tsx';
  const exports = load(file, {
    react: hooks, 'next/link': 'a', 'next/navigation': { useSearchParams: () => current.params },
    'react-markdown': 'markdown', 'remark-breaks': () => {}, 'remark-gfm': () => {},
    '@/lib/paths': { withApiBasePath: (url) => url, withBasePath: (url) => basePath + url }, '@/lib/ask-search-state': helpers,
    '@/components/knowledge-preview': { KnowledgePreview: 'knowledge-preview' },
  }, { window, fetch: (url, init = {}) => { requests.push({ url, init }); return fetcher(url, init); } }, file.startsWith('app/') ? '\nexport { SearchClient, Results };' : '\nexport { DatasetSample };');
  function render() {
    index = 0; dirty = false; tree = exports[name](props);
    for (const effect of pending.splice(0)) effect();
    return tree;
  }
  return {
    current, requests,
    get tree() { if (dirty) render(); return tree; },
    setProps(value) { props = value; dirty = true; },
    navigate(href) { current.location.href = new URL(href, current.location).href; current.params = new URLSearchParams(current.location.search); dirty = true; },
    async settle() { for (let i = 0; i < 40; i++) { if (dirty) render(); await Promise.resolve(); for (const [id, fn] of [...timers]) { timers.delete(id); fn(); } } },
    unmount() { slots.forEach((slot) => slot?.cleanup?.()); mounted = false; },
    get postUnmountWrites() { return postUnmountWrites; },
  };
}

const target = (id = 1) => ({ documentId: id, versionId: id * 10, chunkId: id * 100, title: `资料 ${id}`, sourceType: 'file', sourceUrl: null, page: 2 });
const context = (id = 1, overrides = {}) => ({ document_id: id, document_version_id: id * 10, evidence_type: 'markdown', document_type: 'markdown', title: `资料 ${id}`, heading_path: ['章节一'], page: 2, snippet: `原文 ${id}`, context_markdown: `上下文 ${id}`, preview_url: null, original_url: null, ...overrides });
const dataset = { dataset_id: 8, document_version_id: 10, artifact_version: 3, name: '合成表', sheet_name: 'Sheet1', row_count: 50, column_count: 1, fields: [{ name: 'value', inferred_type: 'text', sample_values: [] }] };

test('native preview reads the exact selected version and offers explicit single-document ask', async () => {
  const h = harness('KnowledgePreview', { target: target(), onClose() {} }, async () => response(context()));
  await h.settle();
  assert.equal(h.requests[0].url, '/api/v1/knowledge/evidence/by-chunk/100?document_version_id=10');
  assert.equal(find(h.tree, 'markdown').props.children, '上下文 1');
  assert.equal(find(h.tree, 'a', '仅问这份资料').props.href, '/ask?document_ids=1');
  assert.equal(find(h.tree, 'iframe'), undefined);
  assert.equal(h.requests.length, 1); h.unmount();
});

test('switching selection immediately hides the previous body and ignores late responses', async () => {
  const first = deferred(), second = deferred();
  const h = harness('KnowledgePreview', { target: target(), onClose() {} }, async (url) => url.includes('/100?') ? first.promise : second.promise);
  await h.settle(); h.setProps({ target: target(2), onClose() {} }); await h.settle();
  second.resolve(response(context(2))); await h.settle();
  first.resolve(response(context())); await h.settle();
  assert.equal(find(h.tree, 'markdown').props.children, '上下文 2');
  assert.equal(h.requests[0].init.signal.aborted, true); h.unmount();
});

test('mismatched document/version is not presented under the selected title', async () => {
  const h = harness('KnowledgePreview', { target: target(), onClose() {} }, async () => response(context(2)));
  await h.settle(); assert.equal(find(h.tree, 'markdown'), undefined); assert.match(text(h.tree), /版本不一致/); h.unmount();
});

test('missing evidence never falls back to a newer version and can be explicitly retried', async () => {
  let failed = true;
  const h = harness('KnowledgePreview', { target: target(), onClose() {} }, async () => failed ? response({}, 409) : response(context()));
  await h.settle(); assert.match(text(h.tree), /不会替换成其他版本/); assert.equal(find(h.tree, 'markdown'), undefined);
  failed = false; find(h.tree, 'button', '重试预览').props.onClick(); await h.settle();
  assert.equal(find(h.tree, 'markdown').props.children, '上下文 1'); h.unmount();
});

test('preview unmount prevents late body commits even when transport ignores abort', async () => {
  const pending = deferred();
  const h = harness('KnowledgePreview', { target: target(), onClose() {} }, async () => ({ ok: true, json: () => pending.promise }));
  await h.settle(); h.unmount(); pending.resolve(context()); await h.settle(); assert.equal(h.postUnmountWrites, 0);
});

test('Markdown rendering rejects active HTML, remote images and unsafe links', async () => {
  const h = harness('KnowledgePreview', { target: { ...target(), sourceUrl: 'javascript:alert(1)' }, onClose() {} }, async () => response(context()));
  await h.settle();
  const markdown = find(h.tree, 'markdown');
  assert.equal(markdown.props.skipHtml, true);
  assert.equal(markdown.props.components.img({ src: 'https://tracker.invalid', alt: '示意图' }).type, 'span');
  assert.equal(markdown.props.components.a({ href: 'javascript:alert(1)', children: 'bad' }).type, 'span');
  assert.equal(markdown.props.components.a({ href: 'https://example.test', children: 'safe' }).props.rel, 'noreferrer noopener');
  assert.equal(find(h.tree, 'a', '打开来源'), undefined); h.unmount();
});

test('file view embeds only the matching version file endpoint, not remote/application URLs', async () => {
  const h = harness('KnowledgePreview', { target: target(), onClose() {} }, async () => response(context(1, { evidence_type: 'pdf_word', document_type: 'pdf', original_url: '/api/documents/1/original?inline=true&version_id=10' })));
  await h.settle(); find(h.tree, 'button', '文件版式').props.onClick();
  assert.equal(find(h.tree, 'iframe').props.src, '/api/documents/1/original?inline=true&version_id=10#page=2');
  assert.equal(find(h.tree, 'iframe').props.sandbox, 'allow-same-origin'); h.unmount();
  const bad = harness('KnowledgePreview', { target: target(), onClose() {} }, async () => response(context(1, { evidence_type: 'pdf_word', document_type: 'pdf', original_url: 'https://remote.invalid/documents/1' })));
  await bad.settle(); assert.equal(find(bad.tree, 'button', '文件版式'), undefined); bad.unmount();
});

test('dataset paging enumerates actual row IDs then loads version-bound raw values', async () => {
  const h = harness('DatasetSample', { dataset, versionId: 10 }, async (url) => url.startsWith('/api/datasets/')
    ? response({ dataset_id: 8, offset: 0, total: 50, rows: [{ row_number: 2, value: 'not rendered' }, { row_number: 19 }, { row_number: 55 }] })
    : response({ dataset_id: 8, document_version_id: 10, columns: ['value'], rows: [{ row_number: 2, value: '核验原值' }] }));
  await h.settle();
  assert.equal(h.requests[1].url, '/api/v1/knowledge/evidence/by-dataset/8/rows?document_version_id=10&artifact_version=3');
  assert.deepEqual(JSON.parse(h.requests[1].init.body).source_rows, [2, 19, 55]);
  assert.match(text(h.tree), /核验原值/); assert.doesNotMatch(text(h.tree), /not rendered/);
  assert.match(text(h.tree), /不是实时源库/); h.unmount();
});

test('dataset page failure clears old values and exposes a same-page retry', async () => {
  let failed = true;
  const h = harness('DatasetSample', { dataset, versionId: 10 }, async (url) => {
    if (url.startsWith('/api/datasets/')) return url.includes('offset=20') && failed ? response({}, 500) : response({ dataset_id: 8, offset: url.includes('offset=20') ? 20 : 0, total: 50, rows: [{ row_number: url.includes('offset=20') ? 22 : 2 }] });
    return response({ dataset_id: 8, document_version_id: 10, columns: ['value'], rows: [{ row_number: 2, value: '样例数据' }] });
  });
  await h.settle(); find(h.tree, 'button', '下一页数据').props.onClick();
  assert.equal(find(h.tree, 'table'), undefined); await h.settle(); assert.equal(find(h.tree, 'table'), undefined);
  assert.match(text(h.tree), /未显示其他表或页码的旧数据/);
  failed = false; find(h.tree, 'button', '重试当前页').props.onClick(); await h.settle(); assert.ok(find(h.tree, 'table')); h.unmount();
});

test('dataset evidence response with the wrong version is rejected', async () => {
  const h = harness('DatasetSample', { dataset, versionId: 10 }, async (url) => url.startsWith('/api/datasets/')
    ? response({ dataset_id: 8, offset: 0, total: 1, rows: [{ row_number: 2 }] })
    : response({ dataset_id: 8, document_version_id: 11, columns: ['value'], rows: [{ value: 'wrong' }] }));
  await h.settle(); assert.equal(find(h.tree, 'table'), undefined); assert.match(text(h.tree), /数据行版本/); h.unmount();
});

test('Escape and close button use the same non-modal return action', async () => {
  let closed = 0, stopped = 0;
  const h = harness('KnowledgePreview', { target: target(), onClose() { closed++; } }, async () => response(context()));
  await h.settle();
  h.tree.props.onKeyDown({ key: 'Escape', stopPropagation() { stopped++; } });
  find(h.tree, 'button', '收起预览').props.onClick();
  assert.equal(closed, 2); assert.equal(stopped, 1); assert.equal(h.tree.props['aria-modal'], undefined); h.unmount();
});

test('opening and closing mobile reading keeps search/page/filter state and restores focus/scroll', async () => {
  const hit = { document_id: 1, document_version_id: 10, title: '合成资料', source_type: 'file', source_url: null, snippet: '原文', highlights: [], categories: [], tags: [], chunk: { id: 100, page: 2, paragraph_index: 0, heading_path: [], type: 'text' } };
  let focusCalls = 0;
  const trigger = { isConnected: true, focus() { focusCalls++; } };
  const h = harness('SearchClient', {}, async (url) => url === '/api/search/filters' ? response({ categories: [], tags: [], source_types: [] }) : response({ query: 'test', total: 100, limit: 20, offset: 40, hits: [hit], retrieval: { mode: 'filters' } }), 'q=test&category=demo&page=3');
  await h.settle();
  nodes(h.tree).find((node) => node.props.payload).props.onPreview(hit, trigger); await h.settle();
  assert.ok(find(h.tree, 'knowledge-preview'));
  assert.ok(nodes(h.tree).some((node) => node.props.className?.includes('hidden lg:block')));
  const queryBefore = h.current.href;
  find(h.tree, 'knowledge-preview').props.onClose(); await h.settle();
  assert.equal(find(h.tree, 'knowledge-preview'), undefined);
  assert.equal(h.current.href, queryBefore); assert.match(h.current.href, /page=3/); assert.match(h.current.href, /category=demo/);
  assert.equal(h.requests.filter((request) => request.url === '/api/search').length, 1);
  assert.equal(focusCalls, 1); assert.equal(h.current.scroll, 640);
  assert.ok(find(h.tree, 'button', '展开阅读区')); h.unmount();
});

test('an outgoing search cannot rewrite a browser-back destination while its effects are still mounted', async () => {
  const h = harness('SearchClient', {}, async (url) => url === '/api/search/filters'
    ? response({ categories: [], tags: [], source_types: [] })
    : response({ query: 'old', total: 0, limit: 20, offset: 0, hits: [] }), 'q=old');
  await h.settle();
  find(h.tree, 'input').props.onChange({ target: { value: 'pending debounce' } });
  // The browser changes its URL before Next commits the route/unmount. A queued
  // debounce and the new useSearchParams value still render the outgoing page.
  const writes = h.current.historyWrites.length;
  const requests = h.requests.length;
  h.navigate('/links/new?draft=1');
  await h.settle();
  assert.equal(h.current.location.pathname, '/links/new');
  assert.equal(h.current.location.search, '?draft=1');
  assert.equal(h.current.historyWrites.length, writes);
  assert.equal(h.requests.length, requests);
  h.unmount();
});

test('search keeps the gateway mount point and skips redundant history replacements', async () => {
  const h = harness('SearchClient', {}, async (url) => url === '/api/search/filters'
    ? response({ categories: [], tags: [], source_types: [] })
    : response({ query: 'old', total: 0, limit: 20, offset: 0, hits: [] }), 'q=old', '/cangzhi');
  await h.settle();
  assert.equal(h.current.historyWrites.length, 0);
  find(h.tree, 'input').props.onChange({ target: { value: 'refined' } });
  await h.settle();
  assert.equal(h.current.location.pathname, '/cangzhi/search');
  assert.equal(h.current.location.search, '?q=refined');
  assert.deepEqual(h.current.historyWrites, ['/cangzhi/search?q=refined']);
  h.unmount();
});
