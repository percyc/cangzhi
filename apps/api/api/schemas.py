from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BlobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sha256: str
    content_type: str
    file_size: int
    original_filename: str | None


class DocumentVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version_number: int
    content_hash: str
    raw_content: str | None
    processing_status: str
    created_at: datetime
    blob: BlobResponse | None = None


class DocumentResponse(BaseModel):
    id: int
    title: str
    description: str | None
    source_type: str
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
    current_version: DocumentVersionResponse | None = None
