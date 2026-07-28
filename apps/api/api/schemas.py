from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BlobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sha256: str
    content_type: str
    file_size: int
    original_filename: str | None


class CategoryMini(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str


class TagMini(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str


class DocumentVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version_number: int
    content_hash: str
    raw_content: str | None
    structured_content: dict[str, Any] | None
    processing_status: str
    created_at: datetime
    blob: BlobResponse | None = None
    source_url: str | None = None


class DocumentSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    summary: str
    confidence: float | None
    model: str | None
    prompt_version: str | None
    source: str
    created_at: datetime


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    source_type: str
    source_url: str | None
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
    current_version: DocumentVersionResponse | None = None
    primary_category: CategoryMini | None = None
    categories: list[CategoryMini] = Field(default_factory=list)
    tags: list[TagMini] = Field(default_factory=list)
    summary: DocumentSummaryResponse | None = None


class ProcessingJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    stage: str
    retry_count: int
    max_retries: int
    last_error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    next_retry_at: datetime | None
    created_at: datetime


class DocumentReprocessResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    success: bool
    message: str
    job_id: int | None = None
