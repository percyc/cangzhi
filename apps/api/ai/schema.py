from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_TAG_COUNT = 5
MAX_TAG_LENGTH = 32
MAX_SUMMARY_LENGTH = 2000
MAX_RATIONALE_LENGTH = 1000
ACCEPTED_CONFIDENCE = (0.0, 1.0)


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
