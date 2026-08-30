import { request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import type { IncomingHttpHeaders, IncomingMessage, RequestOptions, ServerResponse } from 'node:http'

const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
])

function upstreamHeaders(req: IncomingMessage, target: URL): IncomingHttpHeaders {
  const headers: IncomingHttpHeaders = {}
  for (const [name, value] of Object.entries(req.headers)) {
    if (!HOP_BY_HOP.has(name.toLowerCase()) && value !== undefined) headers[name] = value
  }
  headers.host = target.host
  headers['x-forwarded-host'] = req.headers.host
  headers['x-forwarded-proto'] = (req.socket as { encrypted?: boolean }).encrypted ? 'https' : 'http'
  return headers
}

function responseHeaders(headers: IncomingHttpHeaders): IncomingHttpHeaders {
  const forwarded: IncomingHttpHeaders = {}
  for (const [name, value] of Object.entries(headers)) {
    if (!HOP_BY_HOP.has(name.toLowerCase()) && value !== undefined) forwarded[name] = value
  }
  // The page is deliberately embedded by the same DSH origin. Remove legacy
  // frame denial from the private upstream; DSH's own trust fence remains the
  // public boundary.
  delete forwarded['x-frame-options']
  return forwarded
}

export interface ProxyOptions {
  stripPrefix?: string
  upstreamPrefix?: string
  allowFrames?: boolean
  headers?: (request: IncomingMessage) => Promise<IncomingHttpHeaders>
}

export function createProxyHandler(webUrl: string, options: ProxyOptions = {}) {
  const upstream = new URL(webUrl)
  if (upstream.protocol !== 'http:' && upstream.protocol !== 'https:') {
    throw new Error('cangzhi webUrl must use http or https')
  }
  const send = upstream.protocol === 'https:' ? httpsRequest : httpRequest

  return async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const incoming = new URL(req.url ?? '/', 'http://dsh.local')
    const target = new URL(upstream)
    const strippedPath = options.stripPrefix && incoming.pathname.startsWith(options.stripPrefix)
      ? incoming.pathname.slice(options.stripPrefix.length) || '/'
      : incoming.pathname
    target.pathname = `${upstream.pathname.replace(/\/$/, '')}${options.upstreamPrefix ?? ''}${strippedPath}`
    target.search = incoming.search

    const headers = upstreamHeaders(req, target)
    if (options.headers !== undefined) Object.assign(headers, await options.headers(req))
    const requestOptions: RequestOptions = {
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port || undefined,
      method: req.method,
      path: `${target.pathname}${target.search}`,
      headers,
    }
    const proxy = send(requestOptions, (reply) => {
      const headers = responseHeaders(reply.headers)
      if (!options.allowFrames) delete headers['x-frame-options']
      res.writeHead(reply.statusCode ?? 502, headers)
      reply.pipe(res)
    })
    proxy.on('error', (error) => {
      if (res.headersSent) {
        res.destroy(error)
        return
      }
      res.writeHead(502, { 'content-type': 'application/json; charset=utf-8' })
      res.end(JSON.stringify({
        error: 'cangzhi_gateway_unavailable',
        message: '藏知管理服务暂时不可用',
      }))
    })
    req.on('aborted', () => { proxy.destroy() })
    req.pipe(proxy)
  }
}
