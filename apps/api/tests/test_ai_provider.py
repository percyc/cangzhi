"""Tests for the AI provider's answer_question contract."""

from __future__ import annotations

import json
from typing import Any

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
    monkeypatch.setattr("apps.api.ai.provider.httpx.post", transport)
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
    monkeypatch.setattr("apps.api.ai.provider.httpx.post", transport)
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
    monkeypatch.setattr("apps.api.ai.provider.httpx.post", transport)
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
    monkeypatch.setattr("apps.api.ai.provider.httpx.post", transport)
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
    monkeypatch.setattr("apps.api.ai.provider.httpx.post", transport)
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
