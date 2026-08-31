"""Integration tests for the external OCR stage 2 in :class:`PdfParser`.

The tests reuse the PDF helpers from ``test_parsers`` and inject
both a tesseract runner and an external OCR runner so we never
open a real socket. Every test verifies both the legacy fields
(``ocr_engine``, ``source``) and the new ones (``engine``,
``provider``, ``model``, ``external_used``).
"""

from __future__ import annotations

from io import BytesIO
from subprocess import CompletedProcess
from typing import Any

from pypdf import PdfReader, PdfWriter

from apps.api.ocr import ExternalOcrLine, ExternalOcrResult
from apps.api.ocr.base import ExternalOcrError
from apps.api.parsers.pdf import (
    PDF_OCR_ENGINE,
    PDF_OCR_EXTERNAL_ENGINE,
    PdfOcrOptions,
    PdfParser,
    _PageRender,
)
from test_parsers import _make_pdf_with_image, _make_pdf_with_text


def _renderer_factory(pdf_bytes: bytes, _options):
    def _render(_page: int) -> _PageRender:
        return _PageRender(
            width_px=200,
            height_px=200,
            page_width_pt=200.0,
            page_height_pt=200.0,
            png=b"\x89PNG\r\n\x1a\nfake-bytes",
        )

    return _render


def _tsv(text: str, confidence: int = 95) -> bytes:
    header = (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        b"left\ttop\twidth\theight\tconf\ttext\n"
    )
    rows = []
    for index, word in enumerate(text.split()):
        left = 5 + index * 20
        rows.append(
            "5\t1\t1\t1\t1\t%d\t%d\t20\t40\t20\t%d\t%s"
            % (index + 1, left, confidence, word)
        )
    return header + "\n".join(rows).encode()


def _empty_tsv() -> bytes:
    return (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        b"left\ttop\twidth\theight\tconf\ttext\n"
    )


class _RecordingExternalProvider:
    """Minimal :class:`ExternalOcrProvider` for the parser tests."""

    name = "openai"

    def __init__(
        self,
        *,
        result: ExternalOcrResult | None = None,
        raise_value: BaseException | None = None,
        model: str = "vision-test",
    ) -> None:
        self.result = result
        self.raise_value = raise_value
        self.calls: list[dict[str, Any]] = []
        self._model = model

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self._model}

    def recognize(
        self, png_bytes: bytes, *, page_number: int
    ) -> ExternalOcrResult:
        self.calls.append({"page": page_number, "bytes": len(png_bytes)})
        if self.raise_value is not None:
            raise self.raise_value
        assert self.result is not None
        return self.result


def _external_lines(external: _RecordingExternalProvider):
    def _runner(_png, page, _options):
        return external.recognize(b"", page_number=page).lines

    return _runner


# --- the actual tests --------------------------------------------------


def test_high_quality_local_result_skips_external_provider():
    pdf_bytes = _make_pdf_with_image()

    def tesseract_runner(_png, _options):
        return CompletedProcess(
            args=[], returncode=0, stdout=_tsv("a b c"), stderr=b""
        )

    external = _RecordingExternalProvider()
    parser = PdfParser(
        options=PdfOcrOptions(enabled=True, language="eng", min_native_chars=24),
        renderer_factory=_renderer_factory,
        tesseract_runner=tesseract_runner,
        tesseract_lookup=lambda: "/usr/bin/tesseract",
        external_ocr_runner=_external_lines(external),
    )
    result = parser.parse(pdf_bytes)
    extraction = result.structured_content.metadata["pdf_extraction"]

    assert extraction["ocr_candidate_pages"] == [1]
    assert extraction["ocr_completed_pages"] == [1]
    assert extraction.get("external_attempted_pages", []) == []
    assert external.calls == []

    ocr_block = result.structured_content.blocks[0]
    assert ocr_block.extra["source"] == "ocr"
    assert ocr_block.extra["ocr_source"] == "local"
    assert ocr_block.extra["engine"] == PDF_OCR_ENGINE
    assert ocr_block.extra["ocr_engine"] == PDF_OCR_ENGINE
    assert "provider" not in ocr_block.extra


def test_no_text_local_result_triggers_external_provider():
    pdf_bytes = _make_pdf_with_image()

    def tesseract_runner(_png, _options):
        return CompletedProcess(
            args=[], returncode=0, stdout=_empty_tsv(), stderr=b""
        )

    external = _RecordingExternalProvider(
        result=ExternalOcrResult(
            lines=[
                ExternalOcrLine(
                    text="外部识别的内容",
                    confidence=900,
                    bbox=[0, 0, 1000, 1000],
                )
            ],
            provider="openai",
            model="vision-test",
            version="vision-test-2024-07-18",
            raw_text_chars=10,
        )
    )

    parser = PdfParser(
        options=PdfOcrOptions(
            enabled=True,
            language="eng",
            min_native_chars=24,
            external_provider=external,
            external_min_chars=8,
            external_confidence_threshold=600,
        ),
        renderer_factory=_renderer_factory,
        tesseract_runner=tesseract_runner,
        tesseract_lookup=lambda: "/usr/bin/tesseract",
        external_ocr_runner=_external_lines(external),
    )
    result = parser.parse(pdf_bytes)
    extraction = result.structured_content.metadata["pdf_extraction"]

    assert extraction["external_attempted_pages"] == [1]
    assert extraction["external_completed_pages"] == [1]
    assert extraction["external_provider"]["model"] == "vision-test"
    triggers = extraction["external_trigger_reasons"]
    assert triggers["no_text"] == [1]

    assert external.calls and external.calls[0]["page"] == 1
    block = result.structured_content.blocks[0]
    assert block.extra["source"] == "ocr"
    assert block.extra["ocr_source"] == "external"
    assert block.extra["engine"] == PDF_OCR_EXTERNAL_ENGINE
    assert block.extra["provider"] == "openai"
    assert block.extra["model"] == "vision-test"
    assert block.extra["external_used"] is True
    assert block.extra["external_status"] == "completed"
    assert block.text == "外部识别的内容"


def test_low_confidence_local_result_triggers_external_provider():
    pdf_bytes = _make_pdf_with_image()

    def tesseract_runner(_png, _options):
        return CompletedProcess(
            args=[],
            returncode=0,
            stdout=_tsv("a b c", confidence=30),
            stderr=b"",
        )

    external = _RecordingExternalProvider(
        result=ExternalOcrResult(
            lines=[
                ExternalOcrLine(
                    text="better text",
                    confidence=900,
                    bbox=[0, 0, 1000, 1000],
                )
            ],
            provider="openai",
            model="vision-test",
            version=None,
            raw_text_chars=11,
        )
    )

    parser = PdfParser(
        options=PdfOcrOptions(
            enabled=True,
            language="eng",
            min_native_chars=24,
            external_provider=external,
            external_min_chars=0,
            external_confidence_threshold=600,
        ),
        renderer_factory=_renderer_factory,
        tesseract_runner=tesseract_runner,
        tesseract_lookup=lambda: "/usr/bin/tesseract",
        external_ocr_runner=_external_lines(external),
    )
    result = parser.parse(pdf_bytes)
    extraction = result.structured_content.metadata["pdf_extraction"]

    assert extraction["external_attempted_pages"] == [1]
    assert extraction["external_completed_pages"] == [1]
    triggers = extraction["external_trigger_reasons"]
    assert triggers["low_confidence"] == [1]
    block = result.structured_content.blocks[0]
    assert block.extra["source"] == "ocr"
    assert block.extra["ocr_source"] == "external"
    assert block.text == "better text"


def test_external_failure_falls_back_to_local_result():
    pdf_bytes = _make_pdf_with_image()

    def tesseract_runner(_png, _options):
        return CompletedProcess(
            args=[],
            returncode=0,
            stdout=_tsv("a b c", confidence=30),
            stderr=b"",
        )

    external = _RecordingExternalProvider(
        raise_value=ExternalOcrError("upstream timeout", category="timeout")
    )

    parser = PdfParser(
        options=PdfOcrOptions(
            enabled=True,
            language="eng",
            min_native_chars=24,
            external_provider=external,
            external_min_chars=8,
            external_confidence_threshold=600,
        ),
        renderer_factory=_renderer_factory,
        tesseract_runner=tesseract_runner,
        tesseract_lookup=lambda: "/usr/bin/tesseract",
        external_ocr_runner=_external_lines(external),
    )
    result = parser.parse(pdf_bytes)
    extraction = result.structured_content.metadata["pdf_extraction"]

    assert extraction["external_attempted_pages"] == [1]
    assert extraction["external_failed_pages"] == [1]
    assert extraction["external_completed_pages"] == []
    assert extraction["ocr_completed_pages"] == [1]
    block = result.structured_content.blocks[0]
    assert block.extra["source"] == "ocr"
    assert block.extra["ocr_source"] == "local"
    assert block.extra["external_used"] is False
    assert block.extra["external_status"] == "timeout"
    assert block.text == "a b c"


def test_external_page_cap_caps_attempts_per_document():
    writer = PdfWriter()
    for _ in range(4):
        writer.add_page(PdfReader(BytesIO(_make_pdf_with_image())).pages[0])
    out = BytesIO()
    writer.write(out)
    pdf_bytes = out.getvalue()

    def tesseract_runner(_png, _options):
        return CompletedProcess(
            args=[],
            returncode=0,
            stdout=_tsv("a b c", confidence=30),
            stderr=b"",
        )

    external = _RecordingExternalProvider(
        result=ExternalOcrResult(
            lines=[
                ExternalOcrLine(
                    text="vision text",
                    confidence=900,
                    bbox=[0, 0, 1000, 1000],
                )
            ],
            provider="openai",
            model="vision-test",
            version=None,
            raw_text_chars=11,
        )
    )

    parser = PdfParser(
        options=PdfOcrOptions(
            enabled=True,
            language="eng",
            min_native_chars=24,
            external_provider=external,
            external_min_chars=8,
            external_confidence_threshold=600,
            external_max_pages=2,
        ),
        renderer_factory=_renderer_factory,
        tesseract_runner=tesseract_runner,
        tesseract_lookup=lambda: "/usr/bin/tesseract",
        external_ocr_runner=_external_lines(external),
    )
    result = parser.parse(pdf_bytes)
    extraction = result.structured_content.metadata["pdf_extraction"]

    assert extraction["external_attempted_pages"] == [1, 2]
    assert extraction["external_completed_pages"] == [1, 2]
    assert extraction["external_skipped_pages"] == [3, 4]
    assert extraction["external_max_pages"] == 2
    triggers = extraction["external_trigger_reasons"]
    assert triggers["max_external_pages_reached"] == [3, 4]
    assert len(external.calls) == 2


def test_external_provider_with_zero_max_pages_is_disabled():
    pdf_bytes = _make_pdf_with_image()

    def tesseract_runner(_png, _options):
        return CompletedProcess(
            args=[],
            returncode=0,
            stdout=_tsv("a b c", confidence=30),
            stderr=b"",
        )

    external = _RecordingExternalProvider(
        result=ExternalOcrResult(
            lines=[
                ExternalOcrLine(
                    text="vision text",
                    confidence=900,
                    bbox=[0, 0, 1000, 1000],
                )
            ],
            provider="openai",
            model="vision-test",
            version=None,
            raw_text_chars=11,
        )
    )

    parser = PdfParser(
        options=PdfOcrOptions(
            enabled=True,
            language="eng",
            min_native_chars=24,
            external_provider=external,
            external_min_chars=8,
            external_confidence_threshold=600,
            external_max_pages=0,
        ),
        renderer_factory=_renderer_factory,
        tesseract_runner=tesseract_runner,
        tesseract_lookup=lambda: "/usr/bin/tesseract",
        external_ocr_runner=_external_lines(external),
    )
    result = parser.parse(pdf_bytes)
    extraction = result.structured_content.metadata["pdf_extraction"]

    assert extraction.get("external_attempted_pages", []) == []
    assert external.calls == []
    block = result.structured_content.blocks[0]
    assert block.extra["source"] == "ocr"
    assert block.extra["ocr_source"] == "local"
    assert block.extra["external_status"] == "disabled"


def test_external_bbox_is_rescaled_to_pdf_points():
    pdf_bytes = _make_pdf_with_image()

    def tesseract_runner(_png, _options):
        return CompletedProcess(
            args=[],
            returncode=0,
            stdout=_tsv("", confidence=30),
            stderr=b"",
        )

    external = _RecordingExternalProvider(
        result=ExternalOcrResult(
            lines=[
                ExternalOcrLine(
                    text="vision text",
                    confidence=900,
                    bbox=[0, 0, 500, 250],
                )
            ],
            provider="openai",
            model="vision-test",
            version=None,
            raw_text_chars=11,
        )
    )

    parser = PdfParser(
        options=PdfOcrOptions(
            enabled=True,
            language="eng",
            min_native_chars=8,
            external_provider=external,
            external_min_chars=8,
            external_confidence_threshold=600,
        ),
        renderer_factory=_renderer_factory,
        tesseract_runner=tesseract_runner,
        tesseract_lookup=lambda: "/usr/bin/tesseract",
        external_ocr_runner=_external_lines(external),
    )
    result = parser.parse(pdf_bytes)
    block = result.structured_content.blocks[0]
    # External bboxes are top-left normalized; parser storage uses PDF
    # points with Y growing upwards, so y=[0, 50pt] becomes [150, 200].
    assert block.extra["bbox"] == [0.0, 150.0, 100.0, 200.0]


def test_legacy_ocr_engine_field_is_preserved_for_back_compat():
    pdf_bytes = _make_pdf_with_image()

    def tesseract_runner(_png, _options):
        return CompletedProcess(
            args=[],
            returncode=0,
            stdout=_tsv("a b c"),
            stderr=b"",
        )

    parser = PdfParser(
        options=PdfOcrOptions(enabled=True, language="eng", min_native_chars=24),
        renderer_factory=_renderer_factory,
        tesseract_runner=tesseract_runner,
        tesseract_lookup=lambda: "/usr/bin/tesseract",
    )
    result = parser.parse(pdf_bytes)
    block = result.structured_content.blocks[0]
    assert block.extra["ocr_engine"] == PDF_OCR_ENGINE
    assert block.extra["engine"] == PDF_OCR_ENGINE


def test_pdf_with_only_native_text_does_not_record_external_metadata():
    pdf_bytes = _make_pdf_with_text(
        "Enough native text on the page that the parser never considers it a scan candidate"
    )
    external = _RecordingExternalProvider()
    parser = PdfParser(
        options=PdfOcrOptions(enabled=True, language="eng", min_native_chars=24),
        renderer_factory=_renderer_factory,
        tesseract_runner=lambda *_a, **_kw: CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        ),
        tesseract_lookup=lambda: "/usr/bin/tesseract",
        external_ocr_runner=_external_lines(external),
    )
    result = parser.parse(pdf_bytes)
    extraction = result.structured_content.metadata["pdf_extraction"]
    assert "external_attempted_pages" not in extraction
    assert external.calls == []
