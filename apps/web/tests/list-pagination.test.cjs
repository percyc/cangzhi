/* eslint-disable @typescript-eslint/no-require-imports -- Execute actual TypeScript UI logic. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
function load(file) {
  const evaluatedModule = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  vm.runInNewContext(code, { module: evaluatedModule, exports: evaluatedModule.exports, require, FormData: class { constructor(form) { this.form = form; } get() { return this.form.value; } } });
  return evaluatedModule.exports;
}
const { tokenStatus } = load('lib/token-list.ts');
const Pagination = load('components/ListPagination.tsx').default;
function nodes(node) { return node && typeof node === 'object' ? [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)] : []; }
test('token status excludes expired and revoked records from active counts', () => {
  assert.equal(tokenStatus({ expires_at: null, revoked_at: null }, 1000), 'active');
  assert.equal(tokenStatus({ expires_at: '1970-01-01T00:00:01Z', revoked_at: null }, 1000), 'expired');
  assert.equal(tokenStatus({ expires_at: '1970-01-01T00:00:01Z', revoked_at: '2020-01-01' }, 1000), 'revoked');
});
test('pagination clamps last-page deletion, disables empty lists and loading', () => {
  let tree = nodes(Pagination({ page: 3, total: 10, pageSize: 5, onPageChange() {} }));
  assert.equal(tree.find(n => n.type === 'input').props.defaultValue, 2);
  for (const props of [{ total: 0, page: 1 }, { total: 100, page: 2, loading: true }]) {
    tree = nodes(Pagination({ ...props, pageSize: 5, onPageChange() {} }));
    assert.ok(tree.filter(n => n.type === 'button').every(n => n.props.disabled));
  }
});
test('page jump rejects current, fractional, empty and out-of-range inputs', () => {
  const pages = [];
  const tree = nodes(Pagination({ page: 2, total: 100, pageSize: 10, onPageChange: p => pages.push(p) }));
  const submit = tree.find(n => n.type === 'form').props.onSubmit;
  for (const value of ['2', '', '0', '11', '2.5', 'abc', '3']) submit({ preventDefault() {}, currentTarget: { value } });
  assert.deepEqual(pages, [3]);
});
