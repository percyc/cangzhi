/* eslint-disable @typescript-eslint/no-require-imports -- Isolated Node harness executes actual transpiled page handlers. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

// No browser, real API, database or model. Effects and HTTP body completion are
// controlled separately so abort-ignoring late responses can be reproduced.
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const response = (body, ok = true) => ({ ok, status: ok ? 200 : 500, json: async () => body });
const nodes = (element) => !element || typeof element !== 'object' ? [] : [element, ...[element.props?.children].flat(Infinity).flatMap(nodes)];
const text = (element) => typeof element === 'string' || typeof element === 'number' ? String(element) : !element || typeof element !== 'object' ? '' : [element.props?.children].flat(Infinity).map(text).join('');
const find = (tree, type, label) => nodes(tree).find((element) => element.type === type && (label === undefined || text(element).includes(label)));

function harness(name, props, fetcher) {
  const slots = [], effects = [], timers = new Map(), intervals = new Map();
  let index = 0, dirty = true, tree, mounted = true, writesAfterUnmount = 0, timerId = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, position) => Object.is(value, b[position]));
  const react = {
    useState(initial) {
      const key = index++;
      if (!slots[key]) slots[key] = { value: typeof initial === 'function' ? initial() : initial };
      return [slots[key].value, (next) => {
        if (!mounted) writesAfterUnmount++;
        const value = typeof next === 'function' ? next(slots[key].value) : next;
        if (!Object.is(value, slots[key].value)) { slots[key].value = value; dirty = true; }
      }];
    },
    useRef(initial) { const key = index++; slots[key] ??= { current: initial }; return slots[key]; },
    useCallback(callback, deps) {
      const key = index++;
      if (!same(slots[key]?.deps, deps)) slots[key] = { deps, callback };
      return slots[key].callback;
    },
    useEffect(callback, deps) {
      const key = index++;
      if (!same(slots[key]?.deps, deps)) effects.push(() => {
        slots[key]?.cleanup?.();
        slots[key] = { deps, cleanup: callback() };
      });
    },
  };
  const window = {
    location: { search: '' },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn) { intervals.set(++timerId, fn); return timerId; },
    clearInterval(id) { intervals.delete(id); },
    confirm: () => true,
  };
  const filename = path.join(__dirname, '../app/documents/[id]/page.tsx');
  const source = fs.readFileSync(filename, 'utf8') + '\nexport { DocumentDetail, DatasetRows, DatasetWorkspace };';
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText;
  const evaluatedModule = { exports: {} };
  const mocks = {
    react, 'next/navigation': { useParams: () => ({ id: props.documentId }), useRouter: () => ({ push() {}, refresh() {} }) },
    'next/link': () => null, 'react-markdown': () => null, 'remark-breaks': () => null, 'remark-gfm': () => null,
    '@/lib/paths': { withApiBasePath: (value) => value },
    '@/lib/navigation-guard': { useUnsavedChanges() {} },
    '@/components/ChunkingPreview': () => null, '@/components/CurrentChunkPreview': () => null, '@/components/KnowledgeEnhancement': () => null,
  };
  vm.runInNewContext(code, { module: evaluatedModule, exports: evaluatedModule.exports, require: (id) => Object.hasOwn(mocks, id) ? mocks[id] : require(id), window, fetch: fetcher, AbortController, URLSearchParams, console }, { filename });
  function render() {
    index = 0; dirty = false; tree = evaluatedModule.exports[name](props);
    while (effects.length) effects.shift()();
    return tree;
  }
  return {
    get tree() { if (dirty) render(); return tree; },
    get writesAfterUnmount() { return writesAfterUnmount; },
    window,
    render,
    setProps(value) { props = value; dirty = true; },
    async settle() { for (let i = 0; i < 30; i++) { if (dirty) render(); await Promise.resolve(); } },
    timers() { const pending = [...timers.values()]; timers.clear(); pending.forEach((fn) => fn()); },
    poll() { [...intervals.values()].forEach((fn) => fn()); },
    unmount() { slots.forEach((slot) => slot?.cleanup?.()); mounted = false; },
  };
}

const pipeline = (overall = 'processing') => ({ overall_status: overall, keyword_searchable: true, vector_searchable: false, stages: {
  parsing: { status: 'completed', message: '完成' }, understanding: { status: 'processing', message: '整理中' },
  chunking: { status: 'completed', message: '完成', child_chunks: 1 }, embedding: { status: 'disabled', message: '未开启', total: 0, completed: 0, failed: 0 },
} });
const documentFixture = (overrides = {}) => ({ id: 1, title: '服务器标题', source_type: 'file', source_url: null, content_kind: 'document', origin: null,
  created_at: '2026-10-01', current_version: { id: 10, version_number: 1, processing_status: 'ready', meta: {}, raw_content: '正文', structured_content: {}, blob: null, preview_blob: null },
  primary_category: { id: 1, name: '分类一' }, categories: [], tags: [{ id: 1, name: '标签一' }], summary: { summary: '服务器摘要' }, ...overrides,
});
function documentApi(getDocument, calls = [], state = pipeline()) {
  return async (url, init = {}) => {
    calls.push({ url, init });
    if (url === '/api/documents/1') return response(getDocument());
    if (url.endsWith('/latest-job')) return response(null);
    if (url.endsWith('/processing-status')) return response(state);
    if (url === '/api/categories') return response([{ id: 1, name: '分类一' }, { id: 2, name: '分类二' }, { id: 3, name: '分类三' }]);
    if (url === '/api/tags') return response([{ id: 1, name: '标签一' }, { id: 2, name: '标签二' }]);
    if (url.startsWith('/api/datasets?')) return response([]);
    throw new Error(`Unexpected URL: ${url}`);
  };
}
async function mountDocument(fetcher) {
  const page = harness('DocumentDetail', { documentId: '1' }, fetcher);
  page.render(); page.timers(); await page.settle(); return page;
}

test('polling preserves title, summary, tags, unsaved category and selected preview', async () => {
  let doc = documentFixture();
  const page = await mountDocument(documentApi(() => doc));
  find(page.tree, 'button', '编辑信息').props.onClick();
  find(page.tree, 'input').props.onChange({ target: { value: '我的草稿' } });
  find(page.tree, 'textarea').props.onChange({ target: { value: '未保存摘要' } });
  find(page.tree, 'button', '#标签二').props.onClick();
  find(page.tree, 'select').props.onChange({ target: { value: '3' } });
  find(page.tree, 'button', '候选对比').props.onClick();
  doc = documentFixture({ title: 'AI 新标题', summary: { summary: 'AI 新摘要' }, primary_category: { id: 2, name: '分类二' }, tags: [] });
  page.poll(); await page.settle();
  assert.equal(find(page.tree, 'input').props.value, '我的草稿');
  assert.equal(find(page.tree, 'textarea').props.value, '未保存摘要');
  assert.match(find(page.tree, 'button', '#标签二').props.className, /bg-blue-700/);
  assert.equal(find(page.tree, 'select').props.value, 3);
  assert.equal(find(page.tree, 'button', '候选对比').props['aria-selected'], true);
  assert.match(text(page.tree), /未保存内容已保留/);
  find(page.tree, 'button', '取消编辑').props.onClick();
  find(page.tree, 'button', '编辑信息').props.onClick();
  assert.equal(find(page.tree, 'input').props.value, 'AI 新标题');
  assert.equal(find(page.tree, 'textarea').props.value, 'AI 新摘要');
  page.unmount();
});

test('a new content version warns editors but does not reset their active tab', async () => {
  let doc = documentFixture();
  const page = await mountDocument(documentApi(() => doc));
  find(page.tree, 'button', '编辑信息').props.onClick();
  find(page.tree, 'button', '在线切片').props.onClick();
  doc = { ...doc, current_version: { ...doc.current_version, id: 11 } };
  page.poll(); await page.settle();
  assert.match(text(page.tree), /文档版本或服务器整理信息已更新/);
  assert.equal(find(page.tree, 'button', '在线切片').props['aria-selected'], true);
  assert.ok(nodes(page.tree).some((element) => element.key === 'current-1:11'));
  page.unmount();
});

test('late poll response cannot overwrite a successfully saved metadata edit', async () => {
  let doc = documentFixture(), block = false;
  const late = deferred();
  const base = documentApi(() => doc);
  const page = await mountDocument(async (url, init) => {
    if (url === '/api/documents/1' && block) return { ok: true, json: () => late.promise };
    if (url.endsWith('/metadata')) return response(doc);
    if (url.endsWith('/tags') && init?.method === 'PATCH') return response({ ...doc, title: '已保存标题' });
    return base(url, init);
  });
  find(page.tree, 'button', '编辑信息').props.onClick();
  find(page.tree, 'input').props.onChange({ target: { value: '已保存标题' } });
  block = true; page.poll(); await page.settle();
  await find(page.tree, 'button', '保存整理信息').props.onClick();
  late.resolve({ ...doc, title: '迟到的旧标题' }); await page.settle();
  assert.equal(text(find(page.tree, 'h1')), '已保存标题');
  page.unmount();
});

test('unmount cancels document loads even when the mocked transport ignores abort', async () => {
  const late = deferred();
  const base = documentApi(() => documentFixture());
  const page = harness('DocumentDetail', { documentId: '1' }, async (url, init) => url === '/api/documents/1' ? { ok: true, json: () => late.promise } : base(url, init));
  page.render(); page.timers(); await page.settle(); page.unmount();
  late.resolve(documentFixture()); await page.settle();
  assert.equal(page.writesAfterUnmount, 0);
});

test('slow polling stays serial instead of aborting and restarting every three seconds', async () => {
  let block = false, requests = 0;
  const late = deferred(), base = documentApi(() => documentFixture());
  const page = await mountDocument(async (url, init) => {
    if (url === '/api/documents/1') {
      requests++;
      if (block) return { ok: true, json: () => late.promise };
    }
    return base(url, init);
  });
  block = true;
  page.poll(); await page.settle(); page.poll(); page.poll();
  assert.equal(requests, 2);
  late.resolve(documentFixture()); await page.settle();
  page.poll(); await page.settle(); assert.equal(requests, 3);
  page.unmount();
});

test('failure retry uses the stage endpoint and full reprocess requires confirmation', async () => {
  const calls = [], state = pipeline('failed'); state.stages.parsing.status = 'failed';
  const doc = documentFixture(); doc.current_version.processing_status = 'failed';
  const base = documentApi(() => doc, calls, state);
  const page = await mountDocument(async (url, init) => {
    if (init?.method === 'POST') { calls.push({ url, init }); return response({ success: true }); }
    return base(url, init);
  });
  await find(page.tree, 'button', '重试失败阶段').props.onClick(); await page.settle();
  assert.ok(calls.some((call) => call.url.endsWith('/retry-failed')));
  page.window.confirm = () => false;
  find(page.tree, 'button', '完整重新处理').props.onClick(); await page.settle();
  assert.equal(calls.some((call) => call.url.endsWith('/reprocess')), false);
  page.unmount();
});

const rowPage = (dataset_id, offset = 0, value = 'A 数据') => ({ dataset_id, offset, total: 120, columns: ['value'], rows: [{ row_number: offset + 1, value }] });

test('failed next-page load hides old rows and offers same-page retry', async () => {
  let fail = true;
  const page = harness('DatasetRows', { datasetId: 1, identity: '1:10:1:duckdb' }, async (url) => url.includes('offset=50') && fail ? response({}, false) : response(rowPage(1, url.includes('offset=50') ? 50 : 0, url.includes('offset=50') ? '第二页' : '第一页')));
  await page.settle(); assert.match(text(page.tree), /第一页/);
  find(page.tree, 'button', '下一页').props.onClick();
  assert.doesNotMatch(text(page.tree), /第一页/);
  await page.settle();
  assert.equal(find(page.tree, 'table'), undefined);
  assert.match(text(page.tree), /数据预览读取失败/);
  fail = false; find(page.tree, 'button', '重试当前页').props.onClick(); await page.settle();
  assert.match(text(page.tree), /第二页/); assert.match(text(page.tree), /第 51–100 行/);
  page.unmount();
});

test('a late old dataset response is discarded even if abort is ignored', async () => {
  const late = deferred();
  const page = harness('DatasetRows', { datasetId: 1, identity: '1:10:1:duckdb' }, async (url) => url.includes('/1/') ? { ok: true, json: () => late.promise } : response(rowPage(2, 0, 'B 数据')));
  await page.settle();
  page.setProps({ datasetId: 2, identity: '2:10:1:duckdb' }); await page.settle();
  assert.match(text(page.tree), /B 数据/);
  late.resolve(rowPage(1)); await page.settle();
  assert.match(text(page.tree), /B 数据/); assert.doesNotMatch(text(page.tree), /A 数据/);
  page.unmount();
});

test('same dataset with a newer artifact never renders old-version data', async () => {
  const late = deferred(); let count = 0;
  const page = harness('DatasetRows', { datasetId: 1, identity: '1:10:1:duckdb' }, async () => ++count === 1 ? response(rowPage(1)) : { ok: true, json: () => late.promise });
  await page.settle(); assert.match(text(page.tree), /A 数据/);
  page.setProps({ datasetId: 1, identity: '1:10:2:duckdb' });
  assert.doesNotMatch(text(page.tree), /A 数据/);
  late.resolve(rowPage(1, 0, '新版数据')); await page.settle();
  assert.match(text(page.tree), /新版数据/); page.unmount();
});

test('response dataset/page mismatch is rejected instead of being shown as current', async () => {
  const page = harness('DatasetRows', { datasetId: 2, identity: '2:10:1:duckdb' }, async () => response(rowPage(1)));
  await page.settle();
  assert.equal(find(page.tree, 'table'), undefined); assert.match(text(page.tree), /不一致/);
  page.unmount();
});

test('new dataset fetch failure never falls back to previous dataset rows', async () => {
  const page = harness('DatasetRows', { datasetId: 1, identity: '1:10:1:duckdb' }, async (url) => url.includes('/1/') ? response(rowPage(1)) : response({}, false));
  await page.settle(); assert.match(text(page.tree), /A 数据/);
  page.setProps({ datasetId: 2, identity: '2:10:1:duckdb' }); await page.settle();
  assert.equal(find(page.tree, 'table'), undefined); assert.doesNotMatch(text(page.tree), /A 数据/);
  assert.match(text(page.tree), /重试当前页/); page.unmount();
});

test('empty preview reports no rows instead of an impossible 1–0 interval', async () => {
  const page = harness('DatasetRows', { datasetId: 1, identity: '1:10:1:duckdb' }, async () => response({ ...rowPage(1), total: 0, rows: [] }));
  await page.settle(); assert.match(text(page.tree), /暂无数据行/); assert.doesNotMatch(text(page.tree), /1–0/);
  page.unmount();
});

test('dataset list follows parent refresh and gives previews version-specific keys', async () => {
  const dataset = (id, version) => ({ id, document_version_id: version, name: `表 ${id}`, row_count: 120, column_count: 1, sheet_name: 'Sheet1', fields: [], profile: {}, execution: { backend: 'duckdb', artifact_version: 1 }, source_freshness: {} });
  const page = harness('DatasetWorkspace', { datasets: [dataset(1, 10), dataset(2, 10)] }, async () => { throw new Error('Unexpected request'); });
  await page.settle();
  const previewElement = () => nodes(page.tree).find((element) => element.type?.name === 'DatasetRows');
  assert.equal(previewElement().key, '1:10:1:duckdb');
  find(page.tree, 'select').props.onChange({ target: { value: '2' } });
  assert.equal(previewElement().key, '2:10:1:duckdb');
  page.setProps({ datasets: [dataset(3, 11)] }); await page.settle();
  assert.equal(previewElement().key, '3:11:1:duckdb');
  assert.equal(previewElement().props.datasetId, 3); page.unmount();
});

test('database refresh errors clearly identify the retained snapshot as not current source data', async () => {
  const dataset = { id: 1, document_version_id: 10, name: '表 1', row_count: 10, column_count: 0, sheet_name: 'Sheet1', fields: [], profile: {}, execution: { backend: 'duckdb', artifact_version: 1 },
    source_freshness: { kind: 'database_snapshot', stale: true, refresh_mode: 'background', last_error: '源表为空' } };
  const page = harness('DatasetWorkspace', { datasets: [dataset] }, async () => { throw new Error('Unexpected request'); });
  await page.settle(); assert.match(text(page.tree), /源表为空/); assert.match(text(page.tree), /已有快照已保留/); assert.match(text(page.tree), /不代表源库最新数据/);
  page.unmount();
});
