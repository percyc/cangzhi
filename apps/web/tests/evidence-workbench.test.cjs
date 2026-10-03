/* eslint-disable @typescript-eslint/no-require-imports -- Actual TSX is evaluated against isolated hooks and HTTP doubles. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const json = body => ({ ok: true, json: async () => body });
const citation = { id: 1, document_id: 11, document_version_id: 12, chunk_id: 13, title: 'Synthetic evidence', heading_path: [], page: 3, paragraph_index: 0, source_type: 'file', source_url: null, snippet: 'Saved evidence', evidence_type: 'document' };
const context = { evidence_type: 'document', document_id: 11, document_version_id: 12, title: 'Synthetic evidence', heading_path: [], page: 3, snippet: 'Resolved evidence', context_markdown: 'Original context', preview_url: null, original_url: null, document_type: 'text' };
function nodes(element) {
  if (!element || typeof element !== 'object') return [];
  return [element, ...[element.props?.children].flat(Infinity).flatMap(nodes)];
}
function text(element) { return element && typeof element === 'object' ? [element.props?.children].flat(Infinity).map(text).join('') : element == null ? '' : String(element); }
function reader(selected, fetcher) {
  const slots = [], effects = [], calls = [];
  let cursor = 0, dirty = true, tree, writes = 0, closes = 0, active = true;
  const hooks = {
    useState(initial) { const key = cursor++; if (!(key in slots)) slots[key] = typeof initial === 'function' ? initial() : initial; return [slots[key], value => { writes++; const next = typeof value === 'function' ? value(slots[key]) : value; if (!Object.is(next, slots[key])) { slots[key] = next; dirty = true; } }]; },
    useRef(initial) { const key = cursor++; return slots[key] ??= { current: initial }; },
    useMemo(fn, deps) { const key = cursor++; if (!slots[key] || deps.some((dep, index) => !Object.is(dep, slots[key].deps[index]))) slots[key] = { deps, value: fn() }; return slots[key].value; },
    useEffect(fn, deps) { const key = cursor++, previous = slots[key]; if (!previous || deps.some((dep, index) => !Object.is(dep, previous.deps[index]))) { slots[key] = { deps, cleanup: previous?.cleanup }; effects.push(() => { slots[key].cleanup?.(); slots[key].cleanup = fn(); }); } },
  };
  const filename = path.join(__dirname, '../components/evidence-drawer.tsx');
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText;
  const evaluatedModule = { exports: {} };
  const mocks = { react: hooks, 'next/link': 'a', 'react-markdown': 'markdown', 'remark-breaks': () => {}, 'remark-gfm': () => {}, '@/lib/paths': { withApiBasePath: value => value } };
  vm.runInNewContext(code, { module: evaluatedModule, exports: evaluatedModule.exports, require: id => Object.hasOwn(mocks, id) ? mocks[id] : require(id), URL, AbortController, fetch: (url, init) => { calls.push({ url, init }); return fetcher(url, init); } }, { filename });
  const component = evaluatedModule.exports.EvidenceDrawer({ citation: selected, onClose: () => {}, active: true });
  const render = () => { cursor = 0; dirty = false; tree = component.type({ citation: selected, onClose: () => { closes++; }, active }); for (const effect of effects.splice(0)) effect(); };
  const flush = async () => { for (let i = 0; i < 30; i++) { await Promise.resolve(); if (dirty) render(); } };
  return {
    flush, calls,
    get tree() { return tree; }, get writes() { return writes; }, get closes() { return closes; },
    click(label) { const button = nodes(tree).find(node => node.type === 'button' && text(node) === label); assert.ok(button, label); button.props.onClick(); },
    setActive(value) { active = value; dirty = true; },
    unmount() { for (const slot of slots) slot?.cleanup?.(); },
  };
}

test('evidence is an inline accessible region, not a body-locking modal', async () => {
  const h = reader(citation, async () => json(context)); await h.flush();
  assert.equal(h.tree.type, 'section'); assert.equal(h.tree.props['aria-label'], '引用证据');
  assert.equal(nodes(h.tree).some(node => node.props.role === 'dialog' || node.props['aria-modal']), false);
  assert.equal(h.tree.props.className.includes('fixed'), false);
  let stopped = false; h.tree.props.onKeyDown({ key: 'Escape', stopPropagation() { stopped = true; } });
  assert.equal(h.closes, 1); assert.equal(stopped, true);
});
test('collapsing and reopening a selected citation preserves its reader without rereading', async () => {
  const h = reader(citation, async () => json(context)); await h.flush(); h.setActive(false); await h.flush(); h.setActive(true); await h.flush();
  assert.equal(h.calls.length, 1); assert.ok(nodes(h.tree).find(node => node.props.context)?.props.context);
});
test('late evidence response after selecting another citation cannot write any stale state', async () => {
  const pending = deferred(), h = reader(citation, () => pending.promise); await h.flush(); h.unmount(); const before = h.writes;
  pending.resolve(json(context)); await h.flush(); assert.equal(h.writes, before);
});
test('late row response after reader close cannot replace a newer citation or clear its loading state', async () => {
  const pending = deferred();
  const h = reader({ ...citation, evidence_type: 'dataset', dataset_id: 6, source_rows: [1] }, async url => url.includes('/by-chunk/') ? json({ ...context, evidence_type: 'dataset', dataset: { dataset_id: 6 } }) : pending.promise);
  await h.flush(); assert.equal(h.calls.length, 2); h.unmount(); const before = h.writes;
  pending.resolve(json({ rows: [{ row_number: 1, value: 'old' }], columns: ['value'] })); await h.flush(); assert.equal(h.writes, before);
});
test('wrong document or version is rejected before showing any evidence', async () => {
  const h = reader(citation, async () => json({ ...context, document_version_id: 99 })); await h.flush();
  assert.match(text(h.tree), /证据版本与引用不一致/); assert.equal(nodes(h.tree).some(node => node.props.context), false);
});
test('failed evidence can retry in place without mixing partially loaded rows', async () => {
  let calls = 0;
  const h = reader(citation, async () => ++calls === 1 ? { ok: false, json: async () => ({ detail: { message: 'temporary failure' } }) } : json(context));
  await h.flush(); assert.match(text(h.tree), /temporary failure/); h.click('重试证据读取'); await h.flush();
  assert.equal(h.calls.length, 2); assert.equal(text(h.tree).includes('temporary failure'), false); assert.equal(nodes(h.tree).find(node => node.props.context)?.props.context.document_version_id, 12);
});
test('only PDF/Word previews use iframes; Markdown API JSON is never embedded as a page', async () => {
  const markdown = reader({ ...citation, evidence_type: 'markdown' }, async () => json({ ...context, evidence_type: 'markdown', document_type: 'markdown', preview_url: '/api/v1/knowledge/documents/11?version_id=12' }));
  await markdown.flush(); assert.equal(nodes(markdown.tree).some(node => node.type === 'iframe'), false);
  const pdf = reader({ ...citation, evidence_type: 'pdf_word' }, async () => json({ ...context, evidence_type: 'pdf_word', document_type: 'pdf', original_url: '/api/documents/11/original?version_id=12' }));
  await pdf.flush(); assert.equal(nodes(pdf.tree).find(node => node.type === 'iframe').props.src, '/api/documents/11/original?version_id=12#page=3');
});
test('foreign URLs and wrong-version preview endpoints cannot be embedded', async () => {
  for (const original_url of ['javascript:alert(1)', 'https://example.test/document.pdf', '/api/documents/11/original?version_id=99', '/api/documents/99/original?version_id=12', '/api/documents/11/original?version_id=12&version_id=99']) {
    const h = reader({ ...citation, evidence_type: 'pdf_word' }, async () => json({ ...context, evidence_type: 'pdf_word', document_type: 'pdf', original_url }));
    await h.flush(); assert.equal(nodes(h.tree).some(node => node.type === 'iframe'), false, original_url);
  }
});
test('markdown evidence cannot autoload third-party images or executable links', async () => {
  const h = reader(citation, async () => json(context)); await h.flush();
  const document = nodes(h.tree).find(node => node.props.context && node.props.citation);
  const markdown = nodes(document.type(document.props)).find(node => node.type === 'markdown');
  const image = markdown.props.components.img({ alt: 'external', src: 'https://tracker.test/a.png' });
  assert.equal(image.type, 'span'); assert.equal(image.props.src, undefined);
  assert.equal(markdown.props.components.a({ href: 'javascript:alert(1)', children: 'unsafe' }).type, 'span');
  const safe = markdown.props.components.a({ href: 'https://example.test/', children: 'source' });
  assert.equal(safe.props.target, '_blank'); assert.equal(safe.props.rel, 'noopener noreferrer');
});
