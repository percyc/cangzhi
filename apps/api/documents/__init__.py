"""Document profiling and type detection module.

Deterministic document type detection based on structure and keywords,
not LLM inference. Provides type-specific chunking configurations.
"""
from .detection import (
    DocumentProfile,
    DocumentType,
    detect_document_type,
)
from .chunking import get_chunking_config, ChunkingConfig

__all__ = [
    "DocumentProfile",
    "DocumentType",
    "detect_document_type",
    "get_chunking_config",
    "ChunkingConfig",
]
