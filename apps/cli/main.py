"""Small dependency-light CLI for Cangzhi's stable knowledge API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

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
    timeout: float = 30.0

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = httpx.request(
                method,
                f"{self.base_url.rstrip('/')}{path}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                    "User-Agent": "cangzhi-cli/0.1",
                },
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
        ("document_ids", True),
        ("source_types", False),
    ):
        value = _comma_values(getattr(args, key, None), integers=integers)
        if value:
            payload[key] = value
    if getattr(args, "scope_id", None):
        payload["scope_id"] = args.scope_id
    if getattr(args, "scope", None):
        payload["scope_slug"] = args.scope
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
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--compact",
        action="store_true",
        help="输出单行 JSON",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="查看服务能力")
    commands.add_parser("scopes", help="列出可用知识范围")

    search = commands.add_parser("search", help="检索知识证据")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    _add_scope_options(search)

    ask = commands.add_parser("ask", help="由藏知模型生成带引用的回答")
    ask.add_argument("question")
    _add_scope_options(ask)

    document = commands.add_parser("document", help="读取当前文档内容")
    document.add_argument("id", type=int)
    chunk = commands.add_parser("chunk", help="读取当前知识片段")
    chunk.add_argument("id", type=int)
    return parser


def run(args: argparse.Namespace, client: CangzhiClient) -> dict[str, Any]:
    if args.command == "capabilities":
        return client.request("GET", "/api/v1/capabilities")
    if args.command == "scopes":
        return client.request("GET", "/api/v1/knowledge/scopes")
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
            payload={"question": args.question, **_scope_payload(args)},
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
