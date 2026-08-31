"""Provider interface and shared data classes for external visual OCR.

The contract is intentionally narrow: a provider receives a PNG
``bytes`` payload (and a logical ``page_number`` for logging) and
returns a list of lines with normalised text, an optional
0..1000 confidence and a 0..1000 top-left normalized bbox. The
parser converts the bbox to its canonical PDF point coordinates.

The provider must:

* never log the API key, the image bytes, the upstream request
  body, or the upstream response body;
* never return a line whose ``text`` is empty or pure whitespace;
* raise :class:`ExternalOcrError` for any non-recoverable
  problem; the parser treats this as "fallback to local result".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExternalOcrLine:
    """A single line returned by the external OCR provider.

    Attributes:
        text: Normalised text. Whitespace inside the line is
            preserved; leading/trailing whitespace is stripped by
            the provider.
        confidence: Optional confidence in the closed range
            ``0..1000`` (already converted from the provider's
            0..1 or 0..100 scale). ``None`` means "not reported".
        bbox: Optional 4-element ``[x0, y0, x1, y1]`` bbox in
            normalized 0..1000 coordinates (origin top-left). The
            parser converts it to PDF points and flips the Y axis.
    """

    text: str
    confidence: int | None = None
    bbox: list[float] | None = None


@dataclass
class ExternalOcrResult:
    """Normalised result returned by :meth:`ExternalOcrProvider.recognize`.

    Attributes:
        lines: Ordered, non-empty lines. Empty results are
            represented as an empty list, not as an exception.
        provider: The provider name as reported by
            :attr:`ExternalOcrProvider.name`; this is mirrored
            onto ``Block.extra.provider`` and
            ``metadata.pdf_extraction`` so the front-end can
            surface it without storing the model name twice.
        model: The model identifier the provider actually used
            (as configured by the user). It is stored verbatim
            because it is non-sensitive.
        version: Optional provider-defined version string
            (e.g. the OpenAI response model version). Kept short
            and never includes the API key.
        raw_text_chars: Total number of non-whitespace characters
            in ``lines``. The parser compares this against the
            configured ``min_chars`` threshold to decide whether
            the external result is worth keeping.
    """

    lines: list[ExternalOcrLine] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    version: str | None = None
    raw_text_chars: int = 0


class ExternalOcrError(RuntimeError):
    """Raised when an external OCR provider cannot honour a request.

    Attributes:
        category: Coarse reason used by the parser to decide
            between fallback ("network"/"upstream"/"invalid"),
            retry ("timeout") and configuration errors
            ("auth"/"config"). The category is the only thing
            the worker is allowed to log.
        provider: Same value as :attr:`ExternalOcrProvider.name`.
    """

    def __init__(
        self,
        message: str,
        *,
        category: str = "upstream",
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.category = category
        self.provider = provider


class ExternalOcrProvider(ABC):
    """Abstract base class for external OCR providers.

    The :attr:`name` is used in :class:`ExternalOcrResult` and
    surfaced to the user. Implementations should keep it short,
    lowercase, and free of any credential.
    """

    name: str = ""

    @abstractmethod
    def recognize(
        self,
        png_bytes: bytes,
        *,
        page_number: int,
    ) -> ExternalOcrResult:
        """Run OCR on the supplied PNG and return structured lines.

        Args:
            png_bytes: The page rendered as PNG. Providers must
                accept the exact bytes (no implicit re-encoding)
                so the same render is used by the local
                tesseract pass.
            page_number: 1-based page number. Used for logging
                and tracing; never written to the user-visible
                result.
        """

    def describe(self) -> dict[str, Any]:
        """Return a small, secret-free description of the provider.

        Used by the worker to log "external OCR used" without
        exposing the model name in the user-visible summary when
        the operator does not want it surfaced.
        """

        return {
            "provider": self.name,
        }
