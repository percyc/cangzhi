from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DatasetFilterInput(BaseModel):
    """Public dataset filter contract shared by REST and MCP.

    ``operator`` is canonical. ``op`` remains accepted because several agent
    clients use that conventional spelling when a nested schema is vague.
    """

    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, max_length=255)
    operator: Literal[
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "starts_with",
        "ends_with",
        "direct_child_of",
        "in",
    ]
    value: Any

    @model_validator(mode="before")
    @classmethod
    def accept_op_alias(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "op" not in value:
            return value
        if "operator" in value and value["operator"] != value["op"]:
            raise ValueError("op 与 operator 不能冲突")
        normalized = dict(value)
        normalized["operator"] = normalized.pop("op")
        return normalized


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
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    blob: BlobResponse | None = None
    preview_blob: BlobResponse | None = None
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


class DocumentOriginResponse(BaseModel):
    kind: str
    label: str
    connector_id: int | None = None
    remote_path: str | None = None
    connector_available: bool = False
    source_status: str | None = None


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    source_type: str
    source_url: str | None
    content_kind: str = "document"
    origin: DocumentOriginResponse | None = None
    is_deleted: bool
    deleted_at: datetime | None = None
    delete_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    current_version: DocumentVersionResponse | None = None
    primary_category: CategoryMini | None = None
    categories: list[CategoryMini] = Field(default_factory=list)
    tags: list[TagMini] = Field(default_factory=list)
    summary: DocumentSummaryResponse | None = None


class DocumentVersionListResponse(BaseModel):
    """Small version projection used by collection views.

    Raw and structured content can be many megabytes, so they must not be
    serialized when the UI only needs a status badge.
    """

    id: int
    version_number: int
    processing_status: str
    created_at: datetime


class ProcessingStageResponse(BaseModel):
    status: str
    message: str
    last_error: str | None = None
    extraction: dict[str, Any] | None = None


class ChunkingStageResponse(ProcessingStageResponse):
    child_chunks: int = 0


class EmbeddingStageResponse(ProcessingStageResponse):
    profile_id: int | None = None
    model: str | None = None
    completed: int = 0
    total: int = 0
    missing: int = 0
    failed: int = 0


class ProcessingStagesResponse(BaseModel):
    parsing: ProcessingStageResponse
    understanding: ProcessingStageResponse
    chunking: ChunkingStageResponse
    embedding: EmbeddingStageResponse


class DocumentPipelineResponse(BaseModel):
    document_id: int
    document_version_id: int
    overall_status: str
    keyword_searchable: bool
    vector_searchable: bool
    stages: ProcessingStagesResponse


class DocumentListItemResponse(BaseModel):
    id: int
    title: str
    description: str | None
    source_type: str
    source_url: str | None
    content_kind: str = "document"
    origin: DocumentOriginResponse | None = None
    is_deleted: bool
    deleted_at: datetime | None = None
    delete_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    current_version: DocumentVersionListResponse | None = None
    primary_category: CategoryMini | None = None
    categories: list[CategoryMini] = Field(default_factory=list)
    tags: list[TagMini] = Field(default_factory=list)
    summary: DocumentSummaryResponse | None = None
    pipeline: DocumentPipelineResponse | None = None


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


class DocumentCategoryUpdateRequest(BaseModel):
    category_id: int


class DocumentBatchActionRequest(BaseModel):
    document_ids: list[int] = Field(min_length=1, max_length=200)


class DocumentVectorRepairResponse(BaseModel):
    documents_requested: int
    documents_eligible: int
    chunks_expected: int
    already_fresh: int
    enqueued: int
    reset: int
    skipped: int


class DocumentBatchOrganizeRequest(DocumentBatchActionRequest):
    category_id: int | None = None
    add_tag_ids: list[int] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def require_an_action(self):
        if self.category_id is None and not self.add_tag_ids:
            raise ValueError("请选择分类或标签")
        return self


class DocumentMetadataUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=1024)
    description: str | None = Field(default=None, max_length=5000)
    summary: str | None = Field(default=None, max_length=20000)


class DocumentTagsUpdateRequest(BaseModel):
    tag_ids: list[int] = Field(default_factory=list, max_length=100)
