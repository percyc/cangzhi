"""Pluggable visual OCR providers.

This package defines the abstract :class:`ExternalOcrProvider` plus
the first concrete implementation (:class:`OpenAICompatibleOcrProvider`).
The interface is deliberately small:

* :meth:`ExternalOcrProvider.recognize` takes a PNG image and returns
  a list of normalised lines.
* Providers never log the API key, the raw image bytes, or the
  upstream JSON body. The :class:`ExternalOcrError` envelope carries
  only a category and a sanitised message; callers can use the
  category to decide between retry, fallback and reporting.

The package is independent of the chat / embedding providers so a
future :class:`OllamaVisionOcrProvider` (or any other compatible
HTTP service) can be added without touching the chat path.
"""

from .base import (
    ExternalOcrError,
    ExternalOcrLine,
    ExternalOcrProvider,
    ExternalOcrResult,
)
from .openai_compatible import OpenAICompatibleOcrProvider
from .factory import build_external_ocr_provider

__all__ = [
    "ExternalOcrError",
    "ExternalOcrLine",
    "ExternalOcrProvider",
    "ExternalOcrResult",
    "OpenAICompatibleOcrProvider",
    "build_external_ocr_provider",
]
