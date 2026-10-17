/* eslint-disable @typescript-eslint/no-require-imports -- Node server tests. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { uploadPath, headersWithoutHop } = require('../server.cjs');

test('only exact upload endpoints bypass Next, at root and gateway paths', () => {
  for (const base of ['', '/_cangzhi']) {
    for (const route of ['/api/files/upload', '/api/v1/documents', '/api/upload']) {
      assert.equal(uploadPath(`${base}${route}?x=1`, base), route);
      assert.equal(uploadPath(route, base), route);
    }
    for (const route of ['/api/auth/login', '/api/v1/documents/1', '/api/files/upload/evil', '/evil/api/upload']) {
      assert.equal(uploadPath(route, base), null);
    }
  }
});

test('streaming retains credentials, workspace, length and content type, removes hop headers', () => {
  const headers = headersWithoutHop({ connection: 'keep-alive, X-Hop', 'x-hop': 'remove',
    'transfer-encoding': 'chunked', authorization: 'synthetic', cookie: 'synthetic',
    'x-cangzhi-workspace': 'synthetic', 'content-length': '123', 'content-type': 'multipart/form-data' });
  assert.equal(headers.connection, undefined);
  assert.equal(headers['x-hop'], undefined);
  assert.equal(headers['transfer-encoding'], undefined);
  assert.equal(headers.authorization, 'synthetic');
  assert.equal(headers.cookie, 'synthetic');
  assert.equal(headers['x-cangzhi-workspace'], 'synthetic');
  assert.equal(headers['content-length'], '123');
});
