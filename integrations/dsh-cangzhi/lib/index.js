import { createServer, request } from "node:http";
import { request as request$1 } from "node:https";
//#region src/proxy.ts
const HOP_BY_HOP = new Set([
	"connection",
	"keep-alive",
	"proxy-authenticate",
	"proxy-authorization",
	"te",
	"trailer",
	"transfer-encoding",
	"upgrade"
]);
function upstreamHeaders(req, target) {
	const headers = {};
	for (const [name, value] of Object.entries(req.headers)) if (!HOP_BY_HOP.has(name.toLowerCase()) && value !== void 0) headers[name] = value;
	headers.host = target.host;
	headers["x-forwarded-host"] = req.headers.host;
	headers["x-forwarded-proto"] = req.socket.encrypted ? "https" : "http";
	return headers;
}
function responseHeaders(headers) {
	const forwarded = {};
	for (const [name, value] of Object.entries(headers)) if (!HOP_BY_HOP.has(name.toLowerCase()) && value !== void 0) forwarded[name] = value;
	delete forwarded["x-frame-options"];
	return forwarded;
}
function createProxyHandler(webUrl, options = {}) {
	const upstream = new URL(webUrl);
	if (upstream.protocol !== "http:" && upstream.protocol !== "https:") throw new Error("cangzhi webUrl must use http or https");
	const send = upstream.protocol === "https:" ? request$1 : request;
	return async (req, res) => {
		const incoming = new URL(req.url ?? "/", "http://dsh.local");
		const target = new URL(upstream);
		const strippedPath = options.stripPrefix && incoming.pathname.startsWith(options.stripPrefix) ? incoming.pathname.slice(options.stripPrefix.length) || "/" : incoming.pathname;
		target.pathname = `${upstream.pathname.replace(/\/$/, "")}${options.upstreamPrefix ?? ""}${strippedPath}`;
		target.search = incoming.search;
		const headers = upstreamHeaders(req, target);
		if (options.headers !== void 0) Object.assign(headers, await options.headers(req));
		const proxy = send({
			protocol: target.protocol,
			hostname: target.hostname,
			port: target.port || void 0,
			method: req.method,
			path: `${target.pathname}${target.search}`,
			headers
		}, (reply) => {
			const headers = responseHeaders(reply.headers);
			if (!options.allowFrames) delete headers["x-frame-options"];
			res.writeHead(reply.statusCode ?? 502, headers);
			reply.pipe(res);
		});
		proxy.on("error", (error) => {
			if (res.headersSent) {
				res.destroy(error);
				return;
			}
			res.writeHead(502, { "content-type": "application/json; charset=utf-8" });
			res.end(JSON.stringify({
				error: "cangzhi_gateway_unavailable",
				message: "藏知管理服务暂时不可用"
			}));
		});
		req.on("aborted", () => {
			proxy.destroy();
		});
		req.pipe(proxy);
	};
}
//#endregion
//#region src/index.ts
const name = "cangzhi";
const inject = [
	"systemPrompt",
	"webServer",
	"connection",
	"credentials"
];
const TOKEN_REF = "CANGZHI_TOKEN";
async function jsonBody(req) {
	const chunks = [];
	let size = 0;
	for await (const chunk of req) {
		const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
		size += buffer.length;
		if (size > 16384) throw new Error("request body is too large");
		chunks.push(buffer);
	}
	const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
	if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("invalid JSON object");
	return value;
}
const GUIDANCE = `Cangzhi is connected as the read-only knowledge system named cangzhi.
Use its mcp__cangzhi__knowledge_* tools whenever the user asks to find, inspect, compare, cite, or answer from their knowledge base.
Prefer knowledge_search for retrieval, knowledge_ask for a synthesized answer with citations, and the dataset schema/preview/query tools for structured data.
Never invent document ids, chunk ids, dataset ids, scope names, evidence, or citations. Discover them with list/search tools first, preserve returned citation metadata, and say clearly when Cangzhi is unavailable or has no supporting result.
The Cangzhi button in the DSH sidebar opens a native DSH knowledge workspace for uploads, documents, categories and processing maintenance.`;
async function apply(ctx, config) {
	const webProxy = createProxyHandler(config.webUrl, { allowFrames: true });
	const apiProxy = createProxyHandler(config.apiUrl, {
		stripPrefix: config.apiRoutePrefix,
		upstreamPrefix: "/api"
	});
	const mcpProxy = createProxyHandler(config.apiUrl, { headers: async () => {
		const resolved = await ctx.credentials.resolve(TOKEN_REF);
		if (resolved === void 0) throw new Error("CANGZHI_TOKEN is not configured");
		return { authorization: `Bearer ${resolved.value}` };
	} });
	const mcpServer = createServer((req, res) => {
		mcpProxy(req, res).catch((error) => {
			if (res.headersSent) {
				res.destroy();
				return;
			}
			res.writeHead(503, {
				"content-type": "application/json; charset=utf-8",
				"retry-after": "5"
			});
			res.end(JSON.stringify({
				error: "cangzhi_mcp_not_configured",
				message: error instanceof Error ? error.message : String(error)
			}));
		});
	});
	await new Promise((resolve, reject) => {
		mcpServer.once("error", reject);
		mcpServer.listen(config.internalMcpPort, "127.0.0.1", () => {
			mcpServer.off("error", reject);
			resolve();
		});
	});
	ctx.effect(() => () => new Promise((resolve) => {
		mcpServer.close(() => resolve());
	}), "cangzhi internal mcp proxy");
	ctx.systemPrompt.section({
		name: "integration:cangzhi",
		order: 155,
		text: GUIDANCE
	});
	ctx.effect(() => ctx.webServer.register({
		kind: "prefix",
		path: config.routePrefix,
		handler: async (req, res) => {
			const rejection = ctx.connection.requestRejection(req);
			if (rejection !== void 0) {
				res.writeHead(rejection, { "cache-control": "no-store" });
				res.end(rejection === 401 ? "unauthorized" : "forbidden");
				return;
			}
			webProxy(req, res);
		}
	}), `cangzhi gateway: ${config.routePrefix}`);
	ctx.effect(() => ctx.webServer.register({
		kind: "prefix",
		path: config.apiRoutePrefix,
		handler: async (req, res) => {
			try {
				const rejection = ctx.connection.requestRejection(req);
				if (rejection !== void 0) {
					res.writeHead(rejection, { "cache-control": "no-store" });
					res.end(rejection === 401 ? "unauthorized" : "forbidden");
					return;
				}
				await apiProxy(req, res);
			} catch (error) {
				res.writeHead(500, { "content-type": "application/json; charset=utf-8" });
				res.end(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }));
			}
		}
	}), `cangzhi api gateway: ${config.apiRoutePrefix}`);
	ctx.effect(() => ctx.webServer.register({
		kind: "exact",
		path: "/_cangzhi-plugin/status",
		handler: async (req, res) => {
			const rejection = ctx.connection.requestRejection(req);
			if (rejection !== void 0) {
				res.writeHead(rejection, { "cache-control": "no-store" });
				res.end(rejection === 401 ? "unauthorized" : "forbidden");
				return;
			}
			res.writeHead(200, {
				"content-type": "application/json; charset=utf-8",
				"cache-control": "no-store"
			});
			const credential = await ctx.credentials.describe(TOKEN_REF);
			res.end(JSON.stringify({
				apiConnected: true,
				mcpConfigured: credential.configured,
				toolCount: 14
			}));
		}
	}), "cangzhi plugin status");
	ctx.effect(() => ctx.webServer.register({
		kind: "exact",
		path: "/_cangzhi-plugin/token",
		handler: async (req, res) => {
			const rejection = ctx.connection.requestRejection(req);
			if (rejection !== void 0) {
				res.writeHead(rejection);
				res.end();
				return;
			}
			if (req.method === "DELETE") {
				try {
					await ctx.credentials.unset(TOKEN_REF);
					res.writeHead(204, { "cache-control": "no-store" });
					res.end();
				} catch (error) {
					res.writeHead(400, { "content-type": "application/json; charset=utf-8" });
					res.end(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }));
				}
				return;
			}
			if (req.method !== "POST") {
				res.writeHead(405, { allow: "POST, DELETE" });
				res.end();
				return;
			}
			try {
				const body = await jsonBody(req);
				const token = typeof body.token === "string" ? body.token.trim() : "";
				if (token.length < 20 || token.length > 4096) throw new Error("访问令牌格式无效");
				const validationUrl = new URL("/api/v1/knowledge/scopes", config.apiUrl);
				const validation = await fetch(validationUrl, { headers: { authorization: `Bearer ${token}` } });
				if (!validation.ok) throw new Error(`藏知拒绝了访问令牌（HTTP ${String(validation.status)}）`);
				await ctx.credentials.set(TOKEN_REF, token);
				res.writeHead(204, { "cache-control": "no-store" });
				res.end();
			} catch (error) {
				res.writeHead(400, {
					"content-type": "application/json; charset=utf-8",
					"cache-control": "no-store"
				});
				res.end(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }));
			}
		}
	}), "cangzhi token setup");
}
//#endregion
export { apply, inject, name };
