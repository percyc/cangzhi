/* eslint-disable @typescript-eslint/no-require-imports */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const filename = path.join(__dirname, '../lib/processing-recovery.ts');
const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const loaded = { exports: {} };
vm.runInThisContext(`(function(exports){${output}\n})`, { filename })(loaded.exports);
const { retryFailedDocuments } = loaded.exports;

test('batch retries failed stages only and reports every partial failure', async () => {
  const calls = [];
  const results = await retryFailedDocuments([1, 2, 3, 4], async (url, options) => {
    calls.push([url, options.method]);
    if (url.includes('/4/')) throw new Error('Synthetic network failure');
    if (url.includes('/3/')) return { ok: false, json: async () => ({ detail: '执行中' }) };
    return { ok: true, json: async () => ({ job_ids: url.includes('/1/') ? [10] : [] }) };
  });
  assert.deepEqual(calls, [1, 2, 3, 4].map(id => [`/api/documents/${id}/retry-failed`, 'POST']));
  assert.equal(results.submitted, 1);
  assert.equal(results.unchanged, 1);
  assert.deepEqual(results.failures.map(item => item.id), [3, 4]);
  assert.match(results.failures[0].message, /执行中/);
});

test('empty batches do not request anything', async () => {
  assert.deepEqual(await retryFailedDocuments([], () => assert.fail('unexpected request')), {
    submitted: 0, unchanged: 0, failures: [],
  });
});

test('invalid error payloads do not expose request inputs', async () => {
  const result = await retryFailedDocuments([7], async () => ({
    ok: false, json: async () => ({ detail: [{ input: 'not-to-display' }] }),
  }));
  assert.equal(result.failures[0].message, '资料 7：重试提交失败');
});
