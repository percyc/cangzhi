/* eslint-disable @typescript-eslint/no-require-imports -- Node streaming HTTP entrypoint. */
const http = require('node:http');
const https = require('node:https');
const next = require('next');

const UPLOAD_PATHS = new Set(['/api/files/upload', '/api/v1/documents', '/api/upload']);
const HOP_HEADERS = ['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade'];

function headersWithoutHop(source) {
  const headers = { ...source };
  const connectionNames = String(headers.connection || '').split(',').map(value => value.trim().toLowerCase());
  for (const name of [...HOP_HEADERS, ...connectionNames]) delete headers[name];
  return headers;
}

function uploadPath(url, basePath) {
  const path = new URL(url, 'http://localhost').pathname;
  const plain = basePath && path.startsWith(`${basePath}/`) ? path.slice(basePath.length) : path;
  return UPLOAD_PATHS.has(plain) ? plain : null;
}

function forwardUpload(req, res, apiUrl, path) {
  const target = new URL(path, apiUrl);
  target.search = new URL(req.url, 'http://localhost').search;
  const headers = headersWithoutHop(req.headers);
  headers.host = target.host;
  // Node pipe preserves backpressure; no body cloning, parsing or buffering.
  const upstream = (target.protocol === 'https:' ? https : http).request(target, {
    method: req.method, headers,
  }, response => {
    res.writeHead(response.statusCode, headersWithoutHop(response.headers));
    response.on('error', () => res.destroy());
    response.pipe(res);
  });
  upstream.on('error', () => {
    if (res.headersSent) return res.destroy();
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ detail: '上传连接中断，请检查网络或代理超时设置后重试。' }));
  });
  req.on('aborted', () => upstream.destroy());
  req.on('error', () => upstream.destroy());
  res.on('close', () => upstream.destroy());
  req.pipe(upstream);
}

async function main() {
  const args = process.argv.slice(2);
  const option = (names, fallback) => {
    const index = args.findIndex(arg => names.includes(arg));
    return index >= 0 ? args[index + 1] : fallback;
  };
  const port = Number(option(['-p', '--port'], process.env.PORT || '3000'));
  const hostname = option(['-H', '--hostname'], '0.0.0.0');
  const dev = args.includes('--dev');
  const app = next({ dev, hostname, port });
  await app.prepare();
  // Production mount point comes from the built artifact, not a drifting env.
  const basePath = dev ? (process.env.CANGZHI_WEB_BASE_PATH || '').replace(/\/$/, '')
    : require('./.next/required-server-files.json').config.basePath;
  const apiUrl = process.env.API_URL || 'http://localhost:8000';
  const handle = app.getRequestHandler();
  const server = http.createServer((req, res) => {
    const path = req.method === 'POST' ? uploadPath(req.url, basePath) : null;
    if (path) return forwardUpload(req, res, apiUrl, path);
    handle(req, res);
  });
  server.on('upgrade', app.getUpgradeHandler());
  server.listen(port, hostname);
}

module.exports = { uploadPath, headersWithoutHop, forwardUpload };
if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
