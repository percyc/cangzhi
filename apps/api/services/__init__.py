"""Shared services, with lazy public exports to avoid provider/service cycles.

Low-level model admission must be importable without importing Q&A (which in
turn imports the model provider). Public names below retain their old API.
"""
from importlib import import_module

_MODULE_EXPORTS = {
    "chunker": ("ChunkSpec", "build_chunk_specs", "chunk_content_hash"),
    "qa": ("AskError", "AskRequest", "AskResult", "Evidence", "QAService", "MAX_QUESTION_LENGTH"),
    "search": ("DEFAULT_LIMIT", "MAX_LIMIT", "SearchHit", "SearchResult",
               "extract_cjk_ngrams", "normalize_query", "search_documents"),
}


def __getattr__(name):
    for module, exports in _MODULE_EXPORTS.items():
        if name in exports:
            value = getattr(import_module(f"{__name__}.{module}"), name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "AskError",
    "AskRequest",
    "AskResult",
    "ChunkSpec",
    "DEFAULT_LIMIT",
    "Evidence",
    "MAX_LIMIT",
    "MAX_QUESTION_LENGTH",
    "QAService",
    "SearchHit",
    "SearchResult",
    "build_chunk_specs",
    "chunk_content_hash",
    "extract_cjk_ngrams",
    "normalize_query",
    "search_documents",
]
