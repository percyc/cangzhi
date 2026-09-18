"""Small dependency-light CLI for Cangzhi's stable knowledge API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

DEFAULT_URL = "http://localhost:8000"


class CLIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "cli_error",
        exit_code: int = 1,
    ):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass
class CangzhiClient:
    base_url: str
    token: str
    workspace: str = "default"
    timeout: float = 30.0

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "cangzhi-cli/0.1",
                "X-Cangzhi-Workspace": self.workspace,
            }
            response = httpx.request(
                method,
                f"{self.base_url.rstrip('/')}{path}",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise CLIError(
                f"无法连接藏知：{exc}",
                code="connection_failed",
                exit_code=2,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise CLIError(
                f"藏知返回了非 JSON 响应（HTTP {response.status_code}）",
                code="invalid_response",
            ) from exc
        if response.is_error:
            detail = data.get("detail", data) if isinstance(data, dict) else {}
            code = detail.get("code", "http_error")
            message = detail.get("message", response.reason_phrase)
            raise CLIError(
                f"{message}（HTTP {response.status_code}）",
                code=code,
                exit_code=3,
            )
        if not isinstance(data, dict):
            raise CLIError(
                "藏知返回的 JSON 不是对象",
                code="invalid_response",
            )
        return data


def _comma_values(value: str | None, *, integers: bool = False) -> list:
    if not value:
        return []
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not integers:
        return values
    try:
        return [int(item) for item in values]
    except ValueError as exc:
        raise CLIError(
            "ID 列表必须使用逗号分隔的整数",
            code="invalid_arguments",
        ) from exc


def _scope_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, integers in (
        ("category_ids", True),
        ("tag_ids", True),
        ("connector_ids", True),
        ("source_types", False),
    ):
        value = _comma_values(getattr(args, key, None), integers=integers)
        if value:
            payload[key] = value
    if getattr(args, "scope_id", None):
        payload["scope_id"] = args.scope_id
    if getattr(args, "scope", None):
        payload["scope_slug"] = args.scope
    scope_keys = _comma_values(getattr(args, "scope_keys", None))
    document_ids = _comma_values(
        getattr(args, "document_ids", None), integers=True
    )
    if scope_keys or document_ids:
        payload["document_selection"] = {
            "scope_keys": scope_keys,
            "document_ids": document_ids,
        }
    return payload


def _add_scope_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scope", help="系统或已保存范围的 slug")
    group.add_argument("--scope-id", type=int, help="已保存范围的 ID")
    parser.add_argument("--category-ids", help="分类 ID，逗号分隔")
    parser.add_argument("--tag-ids", help="标签 ID，逗号分隔")
    parser.add_argument("--source-types", help="来源类型，逗号分隔")
    parser.add_argument("--connector-ids", help="连接器 ID，逗号分隔")
    parser.add_argument("--document-ids", help="文档 ID，逗号分隔")
    parser.add_argument("--scope-keys", help="文档范围键，逗号分隔")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cangzhi",
        description="藏知 Cangzhi｜个人可控的 AI 知识中枢 CLI（标准输出始终为 JSON）",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("CANGZHI_URL", DEFAULT_URL),
        help="API 地址（环境变量 CANGZHI_URL）",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("CANGZHI_TOKEN"),
        help="个人访问令牌（建议使用环境变量 CANGZHI_TOKEN）",
    )
    parser.add_argument(
        "--workspace",
        default=os.getenv("CANGZHI_WORKSPACE", "default"),
        help="工作空间 slug（环境变量 CANGZHI_WORKSPACE，默认 default）",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--compact",
        action="store_true",
        help="输出单行 JSON",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="查看服务能力")
    commands.add_parser("scopes", help="列出可用知识范围")
    commands.add_parser("facets", help="列出分类、标签、来源和连接器筛选项")

    search = commands.add_parser("search", help="检索知识证据")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    _add_scope_options(search)

    ask = commands.add_parser("ask", help="由藏知模型生成带引用的回答")
    ask.add_argument("question")
    ask.add_argument(
        "--deep",
        action="store_true",
        help="启用受控多步检索与数据集精确计算",
    )
    _add_scope_options(ask)

    document = commands.add_parser("document", help="读取当前文档内容")
    document.add_argument("id", type=int)
    chunk = commands.add_parser("chunk", help="读取当前知识片段")
    chunk.add_argument("id", type=int)
    for name, help_text in (("enhancement-overview", "读取已构建的分层概览（不调用模型）"),
                            ("enhancements", "列出文档的已构建增强记录（不调用模型）"),
                            ("enhancement", "分页读取一个增强窗口及其证据（不调用模型）")):
        enhancement = commands.add_parser(name, help=help_text)
        enhancement.add_argument("id", type=int, help="文档 ID" if name == "enhancements" else "增强运行 ID")
        enhancement.add_argument("--offset", type=int, default=0)
        enhancement.add_argument("--limit", type=int, default=20 if name == "enhancements" else 5)
        enhancement.add_argument("--scope-keys", help="文档范围键，逗号分隔")
        enhancement.add_argument("--document-ids", help="文档 ID，逗号分隔；与范围键取并集")
        if name == "enhancement-overview":
            enhancement.add_argument("--node-key", help="省略时读取根节点，例如 L1:0")
        if name == "enhancement":
            enhancement.add_argument("--window-index", type=int, default=0)
            enhancement.add_argument("--view", choices=["summary", "entities", "relations", "events", "evidence"], default="summary")
    for name in ("document-map", "document-block"):
        source = commands.add_parser(name, help="只读原文结构或原文块（不调用模型）")
        source.add_argument("id", type=int, help="文档 ID")
        source.add_argument("--offset", type=int, default=0)
        source.add_argument("--scope-keys", help="逗号分隔的文档范围键")
        source.add_argument("--document-ids", help="逗号分隔的文档 ID，与范围键取并集")
        if name == "document-map":
            source.add_argument("--view", choices=["outline", "blocks"], default="outline")
            source.add_argument("--limit", type=int, default=20)
        else:
            source.add_argument("--block-id", required=True, help="地图返回的原文块 id")
            source.add_argument("--max-chars", type=int, default=4000)
    return parser


def run(args: argparse.Namespace, client: CangzhiClient) -> dict[str, Any]:
    if args.command == "capabilities":
        return client.request("GET", "/api/v1/capabilities")
    if args.command == "scopes":
        return client.request("GET", "/api/v1/knowledge/scopes")
    if args.command == "facets":
        return client.request("GET", "/api/v1/knowledge/facets")
    if args.command in {"document-map", "document-block"}:
        is_map = args.command == "document-map"
        if args.id <= 0 or not 0 <= args.offset <= (1_000_000 if is_map else 2_000_000):
            raise CLIError("文档 ID 或分页范围无效", code="invalid_arguments")
        query = {"offset": args.offset}
        if is_map:
            if not 1 <= args.limit <= 100:
                raise CLIError("limit 必须在 1..100", code="invalid_arguments")
            query.update(view=args.view, limit=args.limit)
        else:
            if not 1 <= args.max_chars <= 12000 or not 1 <= len(args.block_id) <= 160:
                raise CLIError("原文块 ID 或读取长度无效", code="invalid_arguments")
            query.update(block_id=args.block_id, max_chars=args.max_chars)
        keys = _comma_values(args.scope_keys)
        ids = _comma_values(args.document_ids, integers=True)
        selected = args.scope_keys is not None or args.document_ids is not None
        if (selected and not keys and not ids) or any(i <= 0 for i in ids) or len(keys) > 100 or len(ids) > 200:
            raise CLIError("文档选择无效", code="invalid_arguments")
        if keys:
            query["scope_keys"] = keys
        if ids:
            query["document_ids"] = ids
        suffix = "map" if is_map else "block"
        return client.request("GET", f"/api/v1/knowledge/documents/{args.id}/{suffix}?" + urlencode(query, doseq=True))
    if args.command in {"enhancements", "enhancement", "enhancement-overview"}:
        max_limit = 50 if args.command == "enhancements" else 20
        if args.id <= 0 or args.offset < 0 or not 1 <= args.limit <= max_limit:
            raise CLIError("ID、分页范围无效", code="invalid_arguments")
        query = {"offset": args.offset, "limit": args.limit}
        selected = args.scope_keys is not None or args.document_ids is not None
        keys = _comma_values(args.scope_keys)
        ids = _comma_values(args.document_ids, integers=True)
        if (selected and not keys and not ids) or any(identifier <= 0 for identifier in ids):
            raise CLIError("文档选择不能为空，ID 必须为正整数", code="invalid_arguments")
        if keys:
            query["scope_keys"] = keys
        if ids:
            query["document_ids"] = ids
        if args.command == "enhancements":
            path = f"/api/v1/knowledge/documents/{args.id}/enhancements"
        elif args.command == "enhancement-overview":
            path = f"/api/v1/knowledge/enhancements/{args.id}/overview"
            query.pop("offset")
            query.pop("limit")
            if args.node_key is not None:
                query["node_key"] = args.node_key
        else:
            if args.window_index < 0:
                raise CLIError("窗口序号不能为负数", code="invalid_arguments")
            path = f"/api/v1/knowledge/enhancements/{args.id}"
            query.update(window_index=args.window_index, view=args.view)
        return client.request("GET", path + "?" + urlencode(query, doseq=True))
    if args.command == "search":
        return client.request(
            "POST",
            "/api/v1/knowledge/search",
            payload={
                "query": args.query,
                "limit": args.limit,
                "offset": args.offset,
                **_scope_payload(args),
            },
        )
    if args.command == "ask":
        return client.request(
            "POST",
            "/api/v1/knowledge/ask",
            payload={
                "question": args.question,
                "mode": "deep" if args.deep else "quick",
                **_scope_payload(args),
            },
        )
    if args.command == "document":
        return client.request("GET", f"/api/v1/knowledge/documents/{args.id}")
    if args.command == "chunk":
        return client.request("GET", f"/api/v1/knowledge/chunks/{args.id}")
    raise CLIError(f"未知命令：{args.command}", code="unknown_command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.token:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "missing_token",
                        "message": "请设置 CANGZHI_TOKEN 或传入 --token",
                    }
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        result = run(
            args,
            CangzhiClient(
                base_url=args.url,
                token=args.token,
                workspace=args.workspace,
                timeout=args.timeout,
            ),
        )
    except CLIError as exc:
        print(
            json.dumps(
                {
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                    }
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return exc.exit_code
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0
