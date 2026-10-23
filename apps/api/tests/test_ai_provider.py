"""Tests for the AI provider's answer_question contract."""

from __future__ import annotations

import json

import httpx
import pytest

from apps.api.ai import (
    AIProviderError,
    AnswerResult,
    OllamaProvider,
    OpenAICompatibleProvider,
    validate_against_evidence,
)
from apps.api.ai.provider import (
    _format_evidence,
    _parse_answer_and_validate,
)

# --- Schema / validate_against_evidence ----------------------------------


def test_validate_rejects_fabricated_citation():
    result = AnswerResult(
        answer="这是基于证据的回答。",
        citation_ids=[42],
        insufficient_evidence=False,
    )
    with pytest.raises(ValueError, match="不存在的证据"):
        validate_against_evidence(result, allowed_ids={1, 2})


def test_validate_rejects_sufficient_without_citation():
    result = AnswerResult(
        answer="这是回答",
        citation_ids=[],
        insufficient_evidence=False,
    )
    with pytest.raises(ValueError, match="必须至少引用一条"):
        validate_against_evidence(result, allowed_ids={1, 2})


def test_validate_rejects_citation_with_insufficient_flag():
    result = AnswerResult(
        answer="证据不足",
        citation_ids=[1],
        insufficient_evidence=True,
    )
    with pytest.raises(ValueError, match="不应附带引用"):
        validate_against_evidence(result, allowed_ids={1, 2})


def test_validate_accepts_well_formed_answer():
    result = AnswerResult(
        answer="引用 [1] 解释",
        citation_ids=[1],
        insufficient_evidence=False,
    )
    validated = validate_against_evidence(result, allowed_ids={1, 2})
    assert validated.citation_ids == [1]


def test_validate_accepts_insufficient_without_citation():
    result = AnswerResult(
        answer="知识库中暂无相关内容。",
        citation_ids=[],
        insufficient_evidence=True,
    )
    validated = validate_against_evidence(result, allowed_ids={1, 2})
    assert validated.insufficient_evidence is True


def test_answer_schema_normalizes_and_dedupes_citations():
    # Valid list passes through with duplicates removed and string
    # numbers coerced to int.
    result = AnswerResult.model_validate(
        {
            "answer": "回答",
            "citation_ids": [1, 1, "2", 5, 5],
        }
    )
    assert result.citation_ids == [1, 2, 5]


def test_answer_schema_rejects_invalid_citation_values():
    with pytest.raises(ValueError):
        AnswerResult.model_validate(
            {
                "answer": "回答",
                "citation_ids": ["not-a-number"],
            }
        )
    with pytest.raises(ValueError):
        AnswerResult.model_validate(
            {
                "answer": "回答",
                "citation_ids": [0],
            }
        )


# --- parse helper ---------------------------------------------------------


def test_parse_strips_markdown_fence_and_validates_citations():
    text = "```json\n" + json.dumps(
        {
            "answer": "根据证据 1",
            "citation_ids": [1],
            "insufficient_evidence": False,
        }
    ) + "\n```"
    parsed = _parse_answer_and_validate(text, allowed_ids={1})
    assert isinstance(parsed, AnswerResult)
    assert parsed.citation_ids == [1]


def test_parse_rejects_fabrication():
    text = json.dumps(
        {
            "answer": "捏造引用",
            "citation_ids": [9],
            "insufficient_evidence": False,
        }
    )
    with pytest.raises(AIProviderError, match="不存在的证据"):
        _parse_answer_and_validate(text, allowed_ids={1})


def test_parse_safely_drops_citations_from_insufficient_answer():
    text = json.dumps(
        {
            "answer": "现有证据不足以确认。",
            "citation_ids": [1],
            "insufficient_evidence": True,
        }
    )
    parsed = _parse_answer_and_validate(text, allowed_ids={1})
    assert parsed.insufficient_evidence is True
    assert parsed.citation_ids == []


# --- Format evidence ------------------------------------------------------


def test_format_evidence_includes_id_marker_and_metadata():
    rendered = _format_evidence(
        [
            {
                "id": 1,
                "title": "标题",
                "heading_path": ["第一章", "1.1"],
                "snippet": "片段一",
            }
        ]
    )
    assert "[1]" in rendered
    assert "《标题》" in rendered
    assert "第一章 > 1.1" in rendered
    assert "片段一" in rendered


def test_format_evidence_preserves_executed_dataset_context_only():
    rendered = _format_evidence([{
        "id": 2, "snippet": "计算结果：42", "dataset_id": 7,
        "query_plan": {"filters": [{"column": "部门", "operator": "eq", "value": "研发"}], "metric": "sum"},
        "aggregate": {"value": 42}, "match_rows": 3, "truncated": True,
        "aggregate_note": "筛选后计算，单位未标注", "source_rows": [999999],
        "contributions": [{"private": "omit-this"}], "credential": "not-for-model",
    }])
    assert '"column":"部门"' in rendered
    assert '"value":"研发"' in rendered
    assert '"match_rows":3' in rendered
    assert '"truncated":true' in rendered
    assert '"value":42' in rendered
    assert "单位未标注" in rendered
    assert "999999" not in rendered
    assert "omit-this" not in rendered
    assert "not-for-model" not in rendered


def test_format_document_evidence_does_not_render_dataset_metadata():
    rendered = _format_evidence([{
        "id": 1, "snippet": "正文", "query_plan": {"unexpected": "ignored"},
        "aggregate": {"value": 999999},
    }])
    assert "正文" in rendered
    assert "执行上下文" not in rendered
    assert "ignored" not in rendered
    assert "999999" not in rendered


# --- Provider HTTP plumbing ----------------------------------------------


class _StubTransport(httpx.MockTransport):
    """Captures the incoming request and dispatches to ``handler``.

    The instance is callable, so it can be used as a drop-in for
    :func:`httpx.post` after ``monkeypatch``-ing the symbol. The
    wrapper turns ``httpx.post(url, json=body, headers=..., timeout=...)``
    into a single :class:`httpx.Request` so the underlying
    :class:`httpx.MockTransport` can serve it.
    """

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    def __call__(self, url, *, json=None, **_kwargs):
        request = httpx.Request("POST", url, json=json)
        return self.handle_request(request)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return super().handle_request(request)


def _patch_async_transport(monkeypatch, transport):
    client_class = httpx.AsyncClient
    monkeypatch.setattr("apps.api.ai.provider.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=transport, **kwargs))


def _openai_provider(handler) -> tuple[OpenAICompatibleProvider, _StubTransport]:
    transport = _StubTransport(handler)
    provider = OpenAICompatibleProvider(
        base_url="https://example.com/v1",
        api_key="sk-test",
        model="gpt-4o-mini",
        timeout=5.0,
    )
    return provider, transport


def _ollama_provider(handler) -> tuple[OllamaProvider, _StubTransport]:
    transport = _StubTransport(handler)
    provider = OllamaProvider(
        base_url="http://localhost:11434",
        model="llama3.1",
        timeout=5.0,
    )
    return provider, transport


@pytest.mark.parametrize(
    "content",
    [
        '分析完成。\n{"action":"search","query":"菜谱"}\n以上。',
        [
            {"type": "text", "text": "```json\n"},
            {"type": "text", "text": '{"action":"search","query":"菜谱"}\n```'},
        ],
    ],
)
def test_openai_generate_json_accepts_common_content_variants(monkeypatch, content):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    provider, transport = _openai_provider(handler)
    _patch_async_transport(monkeypatch, transport)
    assert provider.generate_json(system="JSON", prompt="search") == {
        "action": "search",
        "query": "菜谱",
    }


def test_openai_answer_question_parses_structured_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content.decode("utf-8"))
        user_message = body["messages"][-1]["content"]
        # Verify the prompt contains the [1] marker and the question.
        assert "[1]" in user_message
        assert "什么是向量检索" in user_message
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "向量检索是把内容表示成向量的检索方式。",
                                    "citation_ids": [1],
                                    "insufficient_evidence": False,
                                    "rationale": "基于第 1 条证据。",
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider, transport = _openai_provider(handler)
    _patch_async_transport(monkeypatch, transport)
    result = provider.answer_question(
        question="什么是向量检索？",
        evidence=[
            {
                "id": 1,
                "title": "检索笔记",
                "heading_path": ["简介"],
                "snippet": "向量检索是把内容用向量表示的检索方法。",
            }
        ],
    )
    assert isinstance(result, AnswerResult)
    assert result.citation_ids == [1]
    assert result.answer


def test_openai_answer_question_rejects_fabricated_citation(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "我编造了引用",
                                    "citation_ids": [42],
                                    "insufficient_evidence": False,
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider, transport = _openai_provider(handler)
    _patch_async_transport(monkeypatch, transport)
    with pytest.raises(AIProviderError, match="不存在的证据"):
        provider.answer_question(
            question="什么是向量检索？",
            evidence=[
                {
                    "id": 1,
                    "title": "检索笔记",
                    "heading_path": [],
                    "snippet": "片段",
                }
            ],
        )


def test_openai_answer_question_rejects_sufficient_without_citation(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "回答内容",
                                    "citation_ids": [],
                                    "insufficient_evidence": False,
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider, transport = _openai_provider(handler)
    _patch_async_transport(monkeypatch, transport)
    with pytest.raises(AIProviderError, match="必须至少引用一条"):
        provider.answer_question(
            question="什么是向量检索？",
            evidence=[{"id": 1, "title": "t", "heading_path": [], "snippet": "s"}],
        )


def test_openai_answer_question_keeps_insufficient_with_no_citation(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "知识库中暂无相关内容。",
                                    "citation_ids": [],
                                    "insufficient_evidence": True,
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider, transport = _openai_provider(handler)
    _patch_async_transport(monkeypatch, transport)
    result = provider.answer_question(
        question="什么是向量检索？",
        evidence=[{"id": 1, "title": "t", "heading_path": [], "snippet": "s"}],
    )
    assert result.insufficient_evidence is True
    assert result.citation_ids == []


def test_openai_provider_not_configured():
    provider = OpenAICompatibleProvider(
        base_url="https://example.com/v1",
        api_key="",
        model="gpt-4o-mini",
        timeout=5.0,
    )
    assert not provider.is_configured()
    with pytest.raises(AIProviderError, match="未配置"):
        provider.answer_question(
            question="q",
            evidence=[{"id": 1, "title": "t", "heading_path": [], "snippet": "s"}],
        )


def test_ollama_answer_question_parses_structured_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["format"] == "json"
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "answer": "根据证据 [1] 回答。",
                            "citation_ids": [1],
                            "insufficient_evidence": False,
                        }
                    )
                }
            },
        )

    provider, transport = _ollama_provider(handler)
    _patch_async_transport(monkeypatch, transport)
    result = provider.answer_question(
        question="什么是向量检索？",
        evidence=[{"id": 1, "title": "t", "heading_path": [], "snippet": "s"}],
    )
    assert result.citation_ids == [1]


def test_ollama_provider_not_configured():
    provider = OllamaProvider(base_url="", model="llama3.1", timeout=5.0)
    assert not provider.is_configured()
    with pytest.raises(AIProviderError, match="未配置"):
        provider.answer_question(
            question="q",
            evidence=[{"id": 1, "title": "t", "heading_path": [], "snippet": "s"}],
        )


# --- Provider error classification (CZ-Q10) ------------------------------


@pytest.mark.parametrize("timeout_type", [TimeoutError, httpx.ReadTimeout, httpx.ConnectTimeout])
def test_post_chat_timeout_is_classified_with_no_request_details(monkeypatch, timeout_type):
    """A timeout must surface as ``timeout`` and never embed the URL or key."""

    from apps.api.ai.provider import _post_chat

    def _explode(*_args, **_kwargs):
        raise timeout_type("connect timeout at https://secret.example.com/v1")

    monkeypatch.setattr(
        "apps.api.services.model_admission.run_sync", lambda _fn: _explode()
    )

    with pytest.raises(AIProviderError) as exc_info:
        _post_chat(
            "https://secret.example.com/v1/chat/completions",
            {},
            {"Authorization": "Bearer sk-LEAK-ME"},
            timeout=0.01,
        )
    assert exc_info.value.category == AIProviderError.CATEGORY_TIMEOUT
    # The exception message is the safe static label; neither the URL
    # nor any key fragment must leak into the user-facing string.
    assert "secret.example.com" not in str(exc_info.value)
    assert "sk-LEAK-ME" not in str(exc_info.value)
    assert "https://" not in str(exc_info.value)


def test_post_chat_network_error_is_classified_with_no_request_details(monkeypatch):
    """A network-level httpx error must be reported as ``network``."""

    from apps.api.ai.provider import _post_chat

    def _explode(*_args, **_kwargs):
        # httpx.ConnectError usually embeds the request URL; never let
        # that string reach the user.
        raise httpx.ConnectError(
            "Cannot connect to host=secret.example.com:443"
        )

    monkeypatch.setattr(
        "apps.api.services.model_admission.run_sync", lambda _fn: _explode()
    )

    with pytest.raises(AIProviderError) as exc_info:
        _post_chat(
            "https://secret.example.com/v1/chat/completions",
            {},
            {"Authorization": "Bearer sk-LEAK-ME"},
            timeout=1.0,
        )
    assert exc_info.value.category == AIProviderError.CATEGORY_NETWORK
    assert "secret.example.com" not in str(exc_info.value)
    assert "sk-LEAK-ME" not in str(exc_info.value)
    assert "ConnectError" not in str(exc_info.value)


def test_openai_answer_question_upstream_http_omits_body(monkeypatch):
    """An upstream HTTP 500 must not propagate the response body."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            content=(
                b"Internal Server Error "
                b"request_id=req-LEAK-ME "
                b"url=https://secret.example.com/v1/chat/completions"
            ),
        )

    provider, transport = _openai_provider(handler)
    _patch_async_transport(monkeypatch, transport)
    with pytest.raises(AIProviderError) as exc_info:
        provider.answer_question(
            question="什么是向量检索？",
            evidence=[{"id": 1, "title": "t", "heading_path": [], "snippet": "s"}],
        )
    assert exc_info.value.category == AIProviderError.CATEGORY_UPSTREAM_HTTP
    # Body and URL must not leak through the user-facing label.
    assert "req-LEAK-ME" not in str(exc_info.value)
    assert "secret.example.com" not in str(exc_info.value)
    assert "Internal Server Error" not in str(exc_info.value)


def test_openai_answer_question_illegal_json_keeps_desensitized_label(monkeypatch):
    """A 200 with non-JSON body stays in the ``illegal_output`` bucket."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>not json at all"
        )

    provider, transport = _openai_provider(handler)
    _patch_async_transport(monkeypatch, transport)
    with pytest.raises(AIProviderError) as exc_info:
        provider.answer_question(
            question="什么是向量检索？",
            evidence=[{"id": 1, "title": "t", "heading_path": [], "snippet": "s"}],
        )
    assert exc_info.value.category == AIProviderError.CATEGORY_ILLEGAL_OUTPUT
    assert "<html>" not in str(exc_info.value)


def test_ai_provider_error_default_category_is_illegal_output():
    """Legacy ``AIProviderError(msg)`` keeps its old shape for back-compat."""

    err = AIProviderError("legacy call site")
    assert err.category == AIProviderError.CATEGORY_ILLEGAL_OUTPUT
    # The category constants are part of the public contract used by the
    # Q&A service for safe-message lookup; they MUST be stable strings.
    assert AIProviderError.CATEGORY_TIMEOUT == "timeout"
    assert AIProviderError.CATEGORY_NETWORK == "network"
    assert AIProviderError.CATEGORY_UPSTREAM_HTTP == "upstream_http"
    assert AIProviderError.CATEGORY_ILLEGAL_OUTPUT == "illegal_output"
