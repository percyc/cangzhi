/* eslint-disable @typescript-eslint/no-require-imports -- Node harness evaluates the actual transpiled request helpers. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const filename = path.join(__dirname, '../lib/webdav-import.ts');
const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const loaded = { exports: {} };
vm.runInThisContext(`(function(exports){${output}\n})`, { filename })(loaded.exports);
const {
  WEBDAV_SELECTION_LIMIT,
  addWebDavSelection,
  isImportableWebDavFile,
  normalizeWebDavSearch,
  normalizeWebDavSyncBatch,
  removeWebDavSelection,
  requestWebDavSyncBatch,
  webDavBreadcrumbs,
  webDavBrowseUrl,
  webDavDisplayPath,
  webDavTotalPages,
} = loaded.exports;

const reply = (body, ok = true) => ({ ok, json: async () => body });
const file = (id, state = 'discovered') => ({ kind: 'file', id, name: `文件${id}.md`, remote_path: `/docs/文件${id}.md`, file_size: 10, state, document_id: null, last_error: null, ignore_reason: null, resume_state: null, missing_count: 0, keep_snapshot: false });

test('browse URL is paged, decodes the directory and implies recursion only for a search', () => {
  const params = url => new URL(url, 'https://example.test').searchParams;
  const plain = params(webDavBrowseUrl(7));
  assert.equal(plain.get('page_size'), '25');
  assert.equal(plain.has('directory'), false);
  assert.equal(plain.has('recursive'), false);
  assert.equal(plain.has('page'), false);
  assert.equal(params(webDavBrowseUrl(7, { directory: '/docs/部门 1', page: 3 })).get('directory'), '/docs/部门 1');
  assert.equal(params(webDavBrowseUrl(7, { page: 3 })).get('page'), '3');
  const searched = params(webDavBrowseUrl(7, { q: '  报告  ', state: 'failed' }));
  assert.equal(searched.get('q'), '报告');
  assert.equal(searched.get('recursive'), 'true');
  assert.equal(searched.get('state'), 'failed');
  // A literal term keeps % and _ intact instead of re-encoding the value.
  assert.equal(params(webDavBrowseUrl(7, { q: '100%_a' })).get('q'), '100%_a');
  assert.equal(webDavBrowseUrl(7, { q: '' }).includes('q='), false);
  assert.equal(webDavBrowseUrl(7, { recursive: true }).includes('recursive=true'), true);
});

test('an over-long search term is refused instead of silently truncated', () => {
  assert.equal(normalizeWebDavSearch('  报告  '), '报告');
  assert.equal(normalizeWebDavSearch('   '), null);
  assert.equal(normalizeWebDavSearch('a'.repeat(200)), 'a'.repeat(200));
  assert.equal(normalizeWebDavSearch('a'.repeat(201)), null);
});

test('selection is capped, deduplicated and never silently extended past the cap', () => {
  let selected = [];
  for (let id = 1; id <= 5; id++) selected = addWebDavSelection(selected, id);
  assert.deepEqual(selected, [1, 2, 3, 4, 5]);
  assert.deepEqual(addWebDavSelection(selected, 3), selected);
  selected = removeWebDavSelection(selected, 3);
  assert.deepEqual(selected, [1, 2, 4, 5]);
  const capped = Array.from({ length: WEBDAV_SELECTION_LIMIT }, (_, index) => index + 1);
  // The helper is pure: the caller keeps its own array unless it adopts the return.
  assert.deepEqual(addWebDavSelection(capped, 99999), capped);
  assert.equal(addWebDavSelection([...capped.slice(0, WEBDAV_SELECTION_LIMIT - 1)], 99999).length, WEBDAV_SELECTION_LIMIT);
});

test('only pending, changed and failed files can be imported from a selection', () => {
  for (const state of ['discovered', 'changed', 'failed']) assert.equal(isImportableWebDavFile(file(1, state)), true);
  for (const state of ['synced', 'ignored', 'missing', 'suspected_missing']) assert.equal(isImportableWebDavFile(file(1, state)), false);
  assert.equal(isImportableWebDavFile({ kind: 'folder', name: 'd', remote_path: '/docs/d', file_count: 1, pending_count: 1, failed_count: 0 }), false);
});

test('selection scope sends only entry ids and an explicit empty list stays empty', async () => {
  const calls = [];
  const fetcher = async (url, init) => { calls.push({ url, body: JSON.parse(init.body) }); return reply({ ok: true, processed: 0, after_id: 0, through_id: 0, has_more: false }); };
  await requestWebDavSyncBatch(7, { type: 'entries', entry_ids: [4, 9] }, {}, fetcher);
  assert.deepEqual(calls[0], { url: '/api/webdav/7/sync-batch', body: { after_id: 0, limit: 1, entry_ids: [4, 9] } });
  await requestWebDavSyncBatch(7, { type: 'entries', entry_ids: [] }, {}, fetcher);
  assert.deepEqual(calls[1].body, { after_id: 0, limit: 1, entry_ids: [] });
  assert.equal('directory' in calls[1].body, false);
});

test('directory and failed-only scopes stay mutually exclusive and carry the cursor', async () => {
  const calls = [];
  const fetcher = async (url, init) => { calls.push(JSON.parse(init.body)); return reply({ ok: true, after_id: 12, through_id: 40, has_more: true }); };
  await requestWebDavSyncBatch(7, { type: 'directory', directory: '/docs/部门' }, { afterId: 12, throughId: 40 }, fetcher);
  assert.deepEqual(calls[0], { after_id: 12, limit: 1, through_id: 40, directory: '/docs/部门' });
  await requestWebDavSyncBatch(7, { type: 'failed', directory: '/docs/部门' }, { afterId: 12, throughId: 40 }, fetcher);
  assert.deepEqual(calls[1], { after_id: 12, limit: 1, through_id: 40, directory: '/docs/部门', failed_only: true });
  assert.equal('entry_ids' in calls[1], false);
  // The very first request omits through_id so the server freezes the bound.
  await requestWebDavSyncBatch(7, { type: 'directory', directory: '/' }, { afterId: 0 }, fetcher);
  assert.equal('through_id' in calls[2], false);
});

test('batch responses normalize counters and surface per-file failures', () => {
  const result = normalizeWebDavSyncBatch({ ok: false, imported: 1, updated: 'x', unchanged: 2, failed: 1, skipped: 0, processed: 3, after_id: 7, through_id: 7, has_more: false, errors: [{ entry_id: 5, message: '合成失败' }, { message: '' }, null] });
  assert.deepEqual(result, {
    ok: false, imported: 1, updated: 0, unchanged: 2, failed: 1, skipped: 0, processed: 3,
    after_id: 7, through_id: 7, has_more: false,
    errors: [{ entry_id: 5, message: '合成失败' }, { entry_id: null, message: '同步失败' }, { entry_id: null, message: '同步失败' }],
  });
  const empty = normalizeWebDavSyncBatch(null);
  assert.equal(empty.after_id, 0);
  assert.equal(empty.through_id, null);
  assert.equal(empty.has_more, false);
  assert.deepEqual(empty.errors, []);
});

test('transport and HTTP failures reject with readable text that keeps already imported work', async () => {
  await assert.rejects(
    () => requestWebDavSyncBatch(7, { type: 'directory', directory: '/' }, {}, async () => { throw new TypeError('network'); }),
    (error) => error instanceof Error && /network/.test(error.message),
  );
  for (const [payload, expected] of [
    [{ detail: '来源已停用' }, /来源已停用/],
    [{ detail: { message: '扫描进行中' } }, /扫描进行中/],
    [{ detail: [{ input: 'private' }] }, /不会回滚/],
  ]) {
    await assert.rejects(
      () => requestWebDavSyncBatch(7, { type: 'directory', directory: '/' }, {}, async () => reply(payload, false)),
      (error) => error instanceof Error && expected.test(error.message),
    );
  }
});

test('breadcrumbs are human readable and stay inside the source root', () => {
  assert.deepEqual(webDavBreadcrumbs('/docs', '/docs', '/docs'), [{ name: '/docs', path: '/docs' }]);
  assert.deepEqual(webDavBreadcrumbs('/docs', '/docs/a/b', '/docs'), [
    { name: '/docs', path: '/docs' },
    { name: 'a', path: '/docs/a' },
    { name: 'b', path: '/docs/a/b' },
  ]);
  assert.deepEqual(webDavBreadcrumbs('/', '/a', '/'), [{ name: '/', path: '/' }, { name: 'a', path: '/a' }]);
  // A path outside the root never fabricates extra levels.
  assert.deepEqual(webDavBreadcrumbs('/docs', '/other/x', '/docs'), [{ name: '/docs', path: '/docs' }]);
  assert.equal(webDavDisplayPath('/docs', '/docs/a/b.md'), 'a/b.md');
  assert.equal(webDavDisplayPath('/docs', '/'), '/');
  assert.equal(webDavDisplayPath('/docs', '/elsewhere/c.md'), '/elsewhere/c.md');
});

test('total pages never collapse below one even for an empty or unknown total', () => {
  assert.equal(webDavTotalPages(0), 1);
  assert.equal(webDavTotalPages(25), 1);
  assert.equal(webDavTotalPages(26), 2);
  assert.equal(webDavTotalPages(-4), 1);
  assert.equal(webDavTotalPages(10, 5), 2);
});