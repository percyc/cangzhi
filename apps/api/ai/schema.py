from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_TAG_COUNT = 5
MAX_TAG_LENGTH = 32
MAX_SUMMARY_LENGTH = 2000
MAX_RATIONALE_LENGTH = 1000
ACCEPTED_CONFIDENCE = (0.0, 1.0)

# --- Q&A (M3-2) -----------------------------------------------------------
# The model must cite evidence using the integer ids we expose in the
# prompt. ``MAX_ANSWER_LENGTH`` and ``MAX_CITATION_COUNT`` keep the model
# output bounded so a runaway model cannot blow up the response size or
# flood the UI with fabricated references.
MAX_ANSWER_LENGTH = 2000
MAX_RATIONALE_LENGTH_QA = 600
MAX_CITATION_COUNT = 8
MAX_CITATION_ID = 2**31 - 1


class UnderstandingResult(BaseModel):
    """The contract every AI understanding call must respect.

    The model output is validated by this schema before any field is
    written to the database. If validation fails, the worker treats the
    call as a graceful no-op and keeps the document available.
    """

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_LENGTH)
    category_slug: str = Field(min_length=1, max_length=64)
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=MAX_RATIONALE_LENGTH)
    doc_type: Literal["article", "note", "tweet", "other"] = "article"

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str]) -> list[str]:
        if not value:
            return []
        seen: list[str] = []
        seen_set: set[str] = set()
        for tag in value:
            if not isinstance(tag, str):
                raise ValueError("tag must be a string")
            cleaned = tag.strip().lower()
            if not cleaned:
                continue
            cleaned = cleaned[:MAX_TAG_LENGTH]
            if cleaned in seen_set:
                continue
            seen.append(cleaned)
            seen_set.add(cleaned)
            if len(seen) >= MAX_TAG_COUNT:
                break
        return seen

    @field_validator("category_slug")
    @classmethod
    def _normalize_slug(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not cleaned:
            raise ValueError("category_slug cannot be empty")
        return cleaned[:64]

    @field_validator("summary")
    @classmethod
    def _trim_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary cannot be empty")
        return cleaned


class AnswerResult(BaseModel):
    """The contract every answer_question call must respect.

    The model output is validated by this schema before any field is
    surfaced to the user. Validation rules (enforced here and in
    ``validate_against_evidence``):

    * ``answer`` is a non-empty string in natural language.
    * ``insufficient_evidence`` is a boolean. When ``True`` the model is
      declaring it cannot answer with the given evidence; ``answer``
      should be a polite explanation and ``citation_ids`` must be empty.
    * ``citation_ids`` is a list of integer evidence ids drawn from the
      prompt. A sufficient answer must cite at least one id. Fabricated
      ids are rejected by ``validate_against_evidence``.
    * ``rationale`` is optional, short and human-readable.
    """

    answer: str = Field(min_length=1, max_length=MAX_ANSWER_LENGTH)
    insufficient_evidence: bool = False
    citation_ids: list[int] = Field(default_factory=list)
    rationale: str = Field(default="", max_length=MAX_RATIONALE_LENGTH_QA)

    @field_validator("answer")
    @classmethod
    def _trim_answer(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("answer cannot be empty")
        return cleaned[:MAX_ANSWER_LENGTH]

    @field_validator("citation_ids")
    @classmethod
    def _normalize_citation_ids(cls, value: list[int]) -> list[int]:
        cleaned: list[int] = []
        seen: set[int] = set()
        for item in value:
            try:
                cid = int(item)
            except (TypeError, ValueError):
                raise ValueError(f"citation id must be an integer: {item!r}") from None
            if cid <= 0 or cid > MAX_CITATION_ID:
                raise ValueError(f"citation id out of range: {cid}")
            if cid in seen:
                continue
            seen.add(cid)
            cleaned.append(cid)
            if len(cleaned) >= MAX_CITATION_COUNT:
                break
        return cleaned


def validate_against_evidence(
    result: AnswerResult,
    *,
    allowed_ids: set[int],
) -> AnswerResult:
    """Reject fabricated citations and inconsistent self-reporting.

    The function enforces the invariants that a caller cannot trust the
    model to police on its own:

    * Citation ids must come from the evidence we supplied. Anything
      else is fabrication, even if the model swears it is correct.
    * If the model says it has insufficient evidence, it must not cite
      anything. A citation plus ``insufficient_evidence=True`` is
      incoherent and the safer behavior is to reject the response.
    * A sufficient answer must cite at least one piece of evidence; a
      bare "I think so" is not allowed.

    Any violation raises ``ValueError`` so the caller can decide how to
    treat the model as failed.
    """

    unknown = [cid for cid in result.citation_ids if cid not in allowed_ids]
    if unknown:
        raise ValueError(
            f"模型引用了不存在的证据 id: {sorted(unknown)}"
        )
    if result.insufficient_evidence and result.citation_ids:
        raise ValueError("声称证据不足时不应附带引用")
    if not result.insufficient_evidence and not result.citation_ids:
        raise ValueError("充分回答必须至少引用一条证据")
    return result
