from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from ..ocr.base import ExternalOcrError, ExternalOcrProvider
from .base import BaseParser, Block, ParserResult, StructuredContent

logger = logging.getLogger(__name__)

PDF_OCR_VERSION = "pdf-hybrid-v2"
PDF_OCR_ENGINE = "tesseract"
PDF_OCR_EXTERNAL_ENGINE = "external-vision"


@dataclass
class PdfOcrOptions:
    """Optional OCR fallback for PDF pages without native selectable text.

    Defaults match the worker's runtime configuration. ``enabled`` gates the
    whole pipeline so unit tests and integrations can run without tesseract or
    pypdfium2 installed.

    The ``external_*`` fields only matter when the parser is also given an
    :class:`apps.api.ocr.ExternalOcrProvider`. They are ignored otherwise so a
    legacy caller (tests, integrations, the API service that never wired the
    worker channel) still works as before.

    * ``external_provider`` is the optional external OCR provider instance.
    * ``external_min_chars`` is the per-page minimum number of
      non-whitespace characters the local tesseract pass must produce
      before the external model is even attempted. Setting it to a
      non-positive value disables the "low text" trigger.
    * ``external_confidence_threshold`` (0..1000) is the threshold below
      which the average local confidence triggers a remote call. A
      non-positive value disables the "low confidence" trigger.
    * ``external_max_pages`` is the per-document cap on external OCR
      invocations. ``0`` disables external calls. A positive value is
      always an upper bound, including when every upstream call fails.
    """

    enabled: bool = True
    language: str = "chi_sim+eng"
    dpi: int = 180
    min_native_chars: int = 24
    max_pages: int = 300
    timeout_seconds: float = 30.0
    external_provider: Any = None
    external_min_chars: int = 8
    external_confidence_threshold: int = 600
    external_max_pages: int = 20

    def normalized(self) -> PdfOcrOptions:
        return PdfOcrOptions(
            enabled=bool(self.enabled),
            language=(self.language or "chi_sim+eng").strip() or "chi_sim+eng",
            dpi=_clamp_int(self.dpi, 72, 600, 180),
            min_native_chars=_clamp_int(self.min_native_chars, 1, 10_000, 24),
            max_pages=_clamp_int(self.max_pages, 1, 50_000, 300),
            timeout_seconds=_clamp_float(self.timeout_seconds, 1.0, 600.0, 30.0),
            external_provider=self.external_provider,
            external_min_chars=_clamp_int(self.external_min_chars, 0, 1_000, 8),
            external_confidence_threshold=_clamp_int(
                self.external_confidence_threshold, 0, 1_000, 600
            ),
            external_max_pages=_clamp_int(self.external_max_pages, 0, 1_000, 20),
        )


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@dataclass
class _PageOcrResult:
    page: int
    lines: list[dict[str, Any]]
    error: str | None = None


@dataclass
class _PageRender:
    width_px: int
    height_px: int
    page_width_pt: float
    page_height_pt: float
    png: bytes


class PdfParser(BaseParser):
    def __init__(
        self,
        options: PdfOcrOptions | None = None,
        *,
        renderer_factory: (
            Callable[
                [bytes, PdfOcrOptions],
                Callable[[int], _PageRender] | None,
            ]
            | None
        ) = None,
        tesseract_runner: (
            Callable[[bytes, PdfOcrOptions], subprocess.CompletedProcess] | None
        ) = None,
        tesseract_lookup: Callable[[], str | None] | None = None,
        external_ocr_runner: (
            Callable[
                [bytes, int, PdfOcrOptions],
                list[dict[str, Any]] | None,
            ]
            | None
        ) = None,
    ) -> None:
        # OCR is enabled explicitly by the worker. Keeping a bare parser
        # native-only preserves its lightweight use in API/tests/integrations.
        self._options = (options or PdfOcrOptions(enabled=False)).normalized()
        self._renderer_factory = renderer_factory
        self._tesseract_runner = tesseract_runner
        self._tesseract_lookup = tesseract_lookup
        self._external_ocr_runner = external_ocr_runner

    def parse(self, content: bytes, content_type: str | None = None) -> ParserResult:
        try:
            import pypdf
        except ImportError:
            return ParserResult(
                success=False,
                error_message="PDF 解析依赖 pypdf 库，请安装 pypdf>=3.0",
                error_details={"import_error": "pypdf not found"},
            )

        options = self._options
        try:
            reader = pypdf.PdfReader(BytesIO(content))
            page_count = len(reader.pages)
        except Exception as exc:
            return ParserResult(
                success=False,
                error_message=f"PDF 解析失败: {exc}",
                error_details={"exception": str(exc)},
            )

        blocks: list[Block] = []
        heading_path: list[str] = []
        native_text_pages: list[int] = []
        image_pages: list[int] = []
        ocr_candidate_pages: list[int] = []
        ocr_completed_pages: list[int] = []
        ocr_failed_pages: list[int] = []
        ocr_skipped_pages: list[int] = []
        ocr_skipped_reasons: dict[str, list[int]] = {}
        external_attempted_pages: list[int] = []
        external_completed_pages: list[int] = []
        external_failed_pages: list[int] = []
        external_skipped_pages: list[int] = []
        external_trigger_reasons: dict[str, list[int]] = {}
        external_provider_summary: dict[str, Any] = {}

        def _record_skip(reason: str, page_number: int) -> None:
            bucket = ocr_skipped_reasons.setdefault(reason, [])
            bucket.append(page_number)

        def _record_trigger(reason: str, page_number: int) -> None:
            bucket = external_trigger_reasons.setdefault(reason, [])
            bucket.append(page_number)

        def _tesseract_path() -> str | None:
            if self._tesseract_lookup is not None:
                return self._tesseract_lookup()
            return shutil.which("tesseract")

        ocr_renderer: Callable[[int], _PageRender] | None = None
        renderer_initialized = False
        ocr_processed = 0
        external_attempted_total = 0
        provider = options.external_provider
        provider_label = _describe_provider(provider)
        external_provider_summary = dict(provider_label)
        external_max_pages = options.external_max_pages
        external_available = (
            provider is not None
            and external_max_pages != 0
            and bool(provider_label.get("provider"))
        )

        for page_idx, page in enumerate(reader.pages):
            page_number = page_idx + 1
            try:
                raw_text = page.extract_text() or ""
            except Exception as exc:
                logger.warning(
                    "pdf_native_text_failed",
                    extra={"page": page_number, "error": str(exc)},
                )
                raw_text = ""

            cleaned_native = _strip_whitespace(raw_text)
            try:
                image_count = _count_page_image_xobjects(page)
            except Exception as exc:
                logger.warning(
                    "pdf_image_probe_failed",
                    extra={"page": page_number, "error": str(exc)},
                )
                image_count = 0
            if image_count:
                image_pages.append(page_number)

            native_block_count = 0
            if cleaned_native:
                native_text_pages.append(page_number)
                native_blocks = _build_native_blocks(
                    raw_text,
                    page_number,
                    heading_path,
                )
                native_block_count = len(native_blocks)
                blocks.extend(native_blocks)
            if len(cleaned_native) >= options.min_native_chars:
                continue
            if not image_count:
                continue
            ocr_candidate_pages.append(page_number)
            if not options.enabled:
                ocr_skipped_pages.append(page_number)
                _record_skip("disabled", page_number)
                continue
            if ocr_processed >= options.max_pages:
                ocr_skipped_pages.append(page_number)
                _record_skip("max_pages_reached", page_number)
                continue
            if not renderer_initialized:
                renderer_initialized = True
                if _tesseract_path() is not None:
                    ocr_renderer = self._build_ocr_renderer(content, options)
            if ocr_renderer is None:
                ocr_skipped_pages.append(page_number)
                _record_skip("dependencies_unavailable", page_number)
                continue
            local_result = self._ocr_page(
                page_number,
                options,
                ocr_renderer,
            )
            ocr_processed += 1
            local_lines: list[dict[str, Any]] = list(local_result.lines)
            local_chars = _sum_line_chars(local_lines)
            local_avg_confidence = _average_confidence(local_lines)
            local_failed = bool(local_result.error) or not local_lines
            local_sufficient = (
                not local_failed
                and local_chars >= options.external_min_chars
                and (
                    local_avg_confidence is None
                    or local_avg_confidence >= _confidence_threshold_to_unit(
                        options.external_confidence_threshold
                    )
                )
            )

            if local_sufficient or not external_available:
                _record_ocr_outcome(
                    blocks=blocks,
                    heading_path=heading_path,
                    page_number=page_number,
                    native_block_count=native_block_count,
                    cleaned_native=cleaned_native,
                    lines=local_lines,
                    source="local",
                    engine=PDF_OCR_ENGINE,
                    provider="",
                    model="",
                    external_used=False,
                    external_status=("skipped" if external_available else "disabled"),
                )
                if local_failed:
                    ocr_failed_pages.append(page_number)
                    _record_skip(local_result.error or "ocr_no_text", page_number)
                else:
                    ocr_completed_pages.append(page_number)
                continue

            if external_attempted_total >= external_max_pages and external_max_pages > 0:
                _record_ocr_outcome(
                    blocks=blocks,
                    heading_path=heading_path,
                    page_number=page_number,
                    native_block_count=native_block_count,
                    cleaned_native=cleaned_native,
                    lines=local_lines,
                    source="local",
                    engine=PDF_OCR_ENGINE,
                    provider="",
                    model="",
                    external_used=False,
                    external_status="skipped_max",
                )
                if local_chars > 0:
                    # Local produced a usable result; the page is
                    # completed even though we did not call the
                    # external model. The cap is recorded on the
                    # external_* counters so the audit trail still
                    # explains why the external channel was
                    # skipped.
                    ocr_completed_pages.append(page_number)
                else:
                    ocr_failed_pages.append(page_number)
                external_skipped_pages.append(page_number)
                _record_trigger("max_external_pages_reached", page_number)
                continue

            trigger = _classify_external_trigger(
                local_failed=local_failed,
                local_chars=local_chars,
                local_avg_confidence=local_avg_confidence,
                min_chars=options.external_min_chars,
                confidence_threshold=options.external_confidence_threshold,
            )
            _record_trigger(trigger, page_number)
            external_attempted_total += 1
            external_attempted_pages.append(page_number)
            external_result = self._run_external_ocr(
                page_number=page_number,
                options=options,
                ocr_renderer=ocr_renderer,
            )
            if external_result.lines is None:
                external_failed_pages.append(page_number)
                _record_ocr_outcome(
                    blocks=blocks,
                    heading_path=heading_path,
                    page_number=page_number,
                    native_block_count=native_block_count,
                    cleaned_native=cleaned_native,
                    lines=local_lines,
                    source="local",
                    engine=PDF_OCR_ENGINE,
                    provider=provider_label.get("provider", ""),
                    model=provider_label.get("model", ""),
                    external_used=False,
                    external_status=external_result.error or "upstream_error",
                )
                if local_chars > 0:
                    # Both channels tried; the local fallback
                    # carries usable text so the page is still
                    # considered completed.
                    ocr_completed_pages.append(page_number)
                else:
                    # Local produced nothing usable and the
                    # external call also failed; the page has
                    # no output and must not be counted as
                    # completed even though the local fallback
                    # block was written to ``blocks``.
                    ocr_failed_pages.append(page_number)
                continue

            external_completed_pages.append(page_number)
            chosen_lines = _pick_better_result(
                local_lines=local_lines,
                external_lines=external_result.lines,
            )
            chosen_source = (
                "external" if chosen_lines is external_result.lines else "local"
            )
            chosen_engine = (
                PDF_OCR_EXTERNAL_ENGINE
                if chosen_source == "external"
                else PDF_OCR_ENGINE
            )
            ocr_completed_pages.append(page_number)
            _record_ocr_outcome(
                blocks=blocks,
                heading_path=heading_path,
                page_number=page_number,
                native_block_count=native_block_count,
                cleaned_native=cleaned_native,
                lines=chosen_lines,
                source=chosen_source,
                engine=chosen_engine,
                provider=provider_label.get("provider", ""),
                model=provider_label.get("model", ""),
                external_used=True,
                external_status="completed",
            )

        pdf_extraction: dict[str, Any] = {
            "version": PDF_OCR_VERSION,
            "engine": PDF_OCR_ENGINE,
            "page_count": page_count,
            "native_text_pages": native_text_pages,
            "image_pages": image_pages,
            "ocr_candidate_pages": ocr_candidate_pages,
            "ocr_completed_pages": ocr_completed_pages,
            "ocr_failed_pages": ocr_failed_pages,
            "ocr_skipped_pages": ocr_skipped_pages,
        }
        if ocr_skipped_reasons:
            pdf_extraction["ocr_skipped_reasons"] = ocr_skipped_reasons
        if external_available or external_attempted_total:
            pdf_extraction["external_provider"] = external_provider_summary
            pdf_extraction["external_attempted_pages"] = external_attempted_pages
            pdf_extraction["external_completed_pages"] = external_completed_pages
            pdf_extraction["external_failed_pages"] = external_failed_pages
            pdf_extraction["external_skipped_pages"] = external_skipped_pages
            if external_trigger_reasons:
                pdf_extraction["external_trigger_reasons"] = external_trigger_reasons
            pdf_extraction["external_max_pages"] = external_max_pages
        if not ocr_candidate_pages:
            pdf_extraction["ocr_status"] = "not_needed"
        elif ocr_completed_pages and (ocr_failed_pages or ocr_skipped_pages):
            pdf_extraction["ocr_status"] = "partial"
        elif ocr_candidate_pages and not ocr_completed_pages and not ocr_failed_pages:
            pdf_extraction["ocr_status"] = "skipped"
        elif ocr_failed_pages and not ocr_completed_pages:
            pdf_extraction["ocr_status"] = "failed"
        elif ocr_completed_pages:
            pdf_extraction["ocr_status"] = "completed"

        metadata: dict[str, Any] = {
            "page_count": page_count,
            "pdf_extraction": pdf_extraction,
        }

        return ParserResult(
            success=True,
            structured_content=StructuredContent(
                document_type="pdf",
                blocks=blocks,
                metadata=metadata,
            ),
        )

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        if content_type and "application/pdf" in content_type.lower():
            return True
        return bool(file_name and file_name.lower().endswith(".pdf"))

    def _build_ocr_renderer(
        self,
        pdf_bytes: bytes,
        options: PdfOcrOptions,
    ) -> Callable[[int], _PageRender] | None:
        if self._renderer_factory is not None:
            return self._renderer_factory(pdf_bytes, options)
        return _default_ocr_renderer(pdf_bytes, options)

    def _ocr_page(
        self,
        page_number: int,
        options: PdfOcrOptions,
        renderer: Callable[[int], _PageRender],
    ) -> _PageOcrResult:
        tesseract = (
            self._tesseract_lookup()
            if self._tesseract_lookup is not None
            else shutil.which("tesseract")
        )
        if tesseract is None:
            return _PageOcrResult(page=page_number, lines=[], error="tesseract_unavailable")
        try:
            render = renderer(page_number)
        except Exception:
            logger.exception("pdf_ocr_render_failed", extra={"page": page_number})
            return _PageOcrResult(page=page_number, lines=[], error="render_failed")
        if self._tesseract_runner is not None:
            try:
                completed = self._tesseract_runner(render.png, options)
            except subprocess.TimeoutExpired:
                return _PageOcrResult(page=page_number, lines=[], error="ocr_timeout")
            except Exception:
                logger.exception("pdf_ocr_runner_failed", extra={"page": page_number})
                return _PageOcrResult(page=page_number, lines=[], error="ocr_failed")
        else:
            try:
                completed = subprocess.run(
                    [tesseract, "stdin", "stdout", "-l", options.language, "--psm", "6", "tsv"],
                    input=render.png,
                    capture_output=True,
                    timeout=options.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return _PageOcrResult(page=page_number, lines=[], error="ocr_timeout")
            except Exception:
                logger.exception("pdf_ocr_process_failed", extra={"page": page_number})
                return _PageOcrResult(page=page_number, lines=[], error="ocr_failed")

        if completed.returncode != 0:
            return _PageOcrResult(
                page=page_number,
                lines=[],
                error=f"ocr_exit_{completed.returncode}",
            )
        lines = _aggregate_tsv_lines(
            completed.stdout,
            image_width_px=render.width_px,
            image_height_px=render.height_px,
            page_width_pt=render.page_width_pt,
            page_height_pt=render.page_height_pt,
        )
        return _PageOcrResult(page=page_number, lines=lines)

    def _run_external_ocr(
        self,
        *,
        page_number: int,
        options: PdfOcrOptions,
        ocr_renderer: Callable[[int], _PageRender],
    ) -> _ExternalOcrPageOutcome:
        """Run the external OCR for one page and shape the result.

        Failures never raise. A network / upstream / auth / parse
        problem is converted into a ``_ExternalOcrPageOutcome``
        with ``lines=None`` and a short, secret-free ``error``
        category; the caller keeps the local tesseract result and
        records the failure on the document summary.
        """

        provider = options.external_provider
        if provider is None:
            return _ExternalOcrPageOutcome(error="disabled")
        try:
            render = ocr_renderer(page_number)
        except Exception:
            logger.exception(
                "external_ocr_render_failed", extra={"page": page_number}
            )
            return _ExternalOcrPageOutcome(error="render_failed")
        if self._external_ocr_runner is not None:
            try:
                raw = self._external_ocr_runner(
                    render.png, page_number, options
                )
            except Exception as exc:
                category = _classify_runner_exception(exc)
                logger.warning(
                    "external_ocr_runner_failed",
                    extra={"page": page_number, "category": category},
                )
                return _ExternalOcrPageOutcome(error=category)
        else:
            try:
                result = provider.recognize(render.png, page_number=page_number)
            except ExternalOcrError as exc:
                logger.warning(
                    "external_ocr_failed",
                    extra={
                        "page": page_number,
                        "category": exc.category,
                        "provider": exc.provider or provider.name,
                    },
                )
                return _ExternalOcrPageOutcome(error=exc.category)
            except Exception:
                logger.exception(
                    "external_ocr_unexpected", extra={"page": page_number}
                )
                return _ExternalOcrPageOutcome(error="invalid")
            raw = result.lines
        internal = _external_lines_to_internal(
            raw,
            page_width_pt=render.page_width_pt,
            page_height_pt=render.page_height_pt,
        )
        if not internal:
            return _ExternalOcrPageOutcome(error="empty_response")
        return _ExternalOcrPageOutcome(lines=internal, error=None)


def native_heading_level(text: str) -> int | None:
    """Conservative textual headings; identifiers/units are not structure."""
    import re
    import unicodedata

    value = unicodedata.normalize("NFKC", text).strip()
    if not value or len(value) > 100:
        return None
    if re.match(r"^第[一二三四五六七八九十百零〇0-9]+[章节篇部](?:分)?\s*[^\W\d]", value):
        return 2 if "节" in value[:12] else 1
    if re.match(r"^(?:Chapter|Section)\s+(?:\d+|[IVX]+)\b\s*[:. -]?\s+[A-Za-z]", value, re.I):
        return 2 if value.lower().startswith("section") else 1
    numbered = re.match(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[\s、)]+([^\W\d].*)$", value)
    if numbered and not re.search(
        r"[=<>≤≥%/^]|\d{3,}|(?<![A-Za-z])\d+\.\d+|\s\d{1,2}(?:\s|$)",
        numbered.group(2),
    ):
        return min(3, numbered.group(1).count(".") + 1)
    # Uppercase alone is not a heading signal: require a phrase, not an acronym,
    # standard number, Roman numeral, unit, formula, or CJK line with one Latin letter.
    words = value.split()
    if len(words) >= 2 and all(re.fullmatch(r"[A-Z]{3,}", word) for word in words):
        return 1
    return None


def _build_native_blocks(
    text: str,
    page_number: int,
    heading_path: list[str],
) -> list[Block]:
    blocks: list[Block] = []
    paragraph_index = 0
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        level = native_heading_level(line)
        if level is not None:
            while len(heading_path) >= level:
                heading_path.pop()
            heading_path.append(line)
            blocks.append(
                Block(
                    type="heading",
                    text=line,
                    heading_path=heading_path.copy(),
                    page=page_number,
                    level=level,
                    paragraph_index=paragraph_index,
                )
            )
        else:
            blocks.append(
                Block(
                    type="paragraph",
                    text=line,
                    heading_path=heading_path.copy(),
                    page=page_number,
                    paragraph_index=paragraph_index,
                )
            )
        paragraph_index += 1
    return blocks


def _strip_whitespace(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace())


def _count_page_image_xobjects(page: Any) -> int:
    """Count raster image XObjects reachable from a page's resources.

    Walks ``/Resources`` -> ``/XObject`` and inspects each entry's
    ``/Subtype`` for ``/Image``. ``pypdf.PageObject.images`` requires Pillow
    to materialize the actual decoded bitmap, so this minimal probe avoids
    pulling Pillow in just to decide whether a page is a candidate for OCR.
    """

    return _count_resource_images(page, seen=set())


def _count_resource_images(container: Any, seen: set[int]) -> int:
    try:
        resources = container.get("/Resources") or {}
        resources = (
            resources.get_object()
            if hasattr(resources, "get_object")
            else resources
        )
        xobjects = resources.get("/XObject") or {}
        xobjects = (
            xobjects.get_object()
            if hasattr(xobjects, "get_object")
            else xobjects
        )
    except Exception:
        return 0

    count = 0
    for value in dict(xobjects).values():
        try:
            obj = value.get_object() if hasattr(value, "get_object") else value
        except Exception:
            obj = value
        identity = id(obj)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            subtype = obj.get("/Subtype")
        except Exception:
            subtype = None
        if subtype == "/Image":
            count += 1
        elif subtype == "/Form":
            count += _count_resource_images(obj, seen)
    return count


def _default_ocr_renderer(
    pdf_bytes: bytes,
    options: PdfOcrOptions,
) -> Callable[[int], _PageRender] | None:
    """Build a closure that renders a PDF page to PNG bytes.

    The closure captures the parsed pypdfium2 document so each render only
    walks the page list once. Both the renderer and ``tesseract`` are
    detected at build time: if either is missing, the caller skips OCR
    without raising so a degraded environment still parses native pages.
    """

    if shutil.which("tesseract") is None:
        return None
    try:
        import pypdfium2 as pdfium  # type: ignore
    except Exception:
        return None

    try:
        document = pdfium.PdfDocument(BytesIO(pdf_bytes))
    except Exception:
        return None

    def _render(page_number: int) -> _PageRender:
        scale = options.dpi / 72.0
        pdf_page = document[page_number - 1]
        try:
            page_width_pt = float(pdf_page.get_width())
            page_height_pt = float(pdf_page.get_height())
        except Exception:
            page_width_pt = 0.0
            page_height_pt = 0.0
        bitmap = pdf_page.render(scale=scale)
        try:
            pil_image = bitmap.to_pil()
            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG")
            if page_width_pt <= 0 or page_height_pt <= 0:
                page_height_pt = pil_image.height / scale
                page_width_pt = pil_image.width / scale
        finally:
            bitmap.close()
            pdf_page.close()
        return _PageRender(
            width_px=pil_image.width,
            height_px=pil_image.height,
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
            png=buffer.getvalue(),
        )

    return _render


def _aggregate_tsv_lines(
    raw_output: bytes,
    *,
    image_width_px: int,
    image_height_px: int,
    page_width_pt: float,
    page_height_pt: float,
) -> list[dict[str, Any]]:
    if not raw_output:
        return []
    try:
        text = raw_output.decode("utf-8", errors="replace")
    except Exception:
        return []
    lines_iter = iter(text.splitlines())
    try:
        header = next(lines_iter)
    except StopIteration:
        return []
    columns = header.split("\t")
    if len(columns) < 12:
        return []

    grouped: dict[tuple[int, int, int], dict[str, Any]] = {}
    for raw_line in lines_iter:
        cells = raw_line.split("\t")
        if len(cells) < len(columns):
            cells.extend([""] * (len(columns) - len(cells)))
        record = dict(zip(columns, cells))
        try:
            level = int(record.get("level", "0") or 0)
            conf = float(record.get("conf", "-1") or -1)
        except ValueError:
            continue
        if level != 5:
            continue
        word = (record.get("text") or "").strip()
        if not word:
            continue
        try:
            block_num = int(record.get("block_num", "0") or 0)
            par_num = int(record.get("par_num", "0") or 0)
            line_num = int(record.get("line_num", "0") or 0)
            left = int(record.get("left", "0") or 0)
            top = int(record.get("top", "0") or 0)
            width = int(record.get("width", "0") or 0)
            height = int(record.get("height", "0") or 0)
        except ValueError:
            continue
        key = (block_num, par_num, line_num)
        entry = grouped.setdefault(
            key,
            {
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
                "words": [],
                "confidences": [],
            },
        )
        entry["left"] = min(entry["left"], left)
        entry["top"] = min(entry["top"], top)
        entry["right"] = max(entry["right"], left + width)
        entry["bottom"] = max(entry["bottom"], top + height)
        entry["words"].append(word)
        if conf >= 0:
            entry["confidences"].append(conf / 100.0)

    ordered = sorted(
        grouped.items(),
        key=lambda item: (item[0][0], item[0][1], item[0][2]),
    )
    aggregated: list[dict[str, Any]] = []
    for index, (_, entry) in enumerate(ordered):
        text_line = _join_ocr_words(entry["words"])
        if not text_line:
            continue
        bbox = _pixel_bbox_to_points(
            entry["left"],
            entry["top"],
            entry["right"],
            entry["bottom"],
            image_width=image_width_px,
            image_height=image_height_px,
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
        )
        aggregated.append(
            {
                "text": text_line,
                "paragraph_index": index,
                "bbox": bbox,
                "confidence": (
                    sum(entry["confidences"]) / len(entry["confidences"])
                    if entry["confidences"]
                    else None
                ),
            }
        )
    return aggregated


_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_NO_SPACE_BEFORE = set(",.;:!?，。；：！？、)]}》」』”")
_NO_SPACE_AFTER = set("([{《「『“")


def _join_ocr_words(words: list[str]) -> str:
    output = ""
    for word in words:
        if not word:
            continue
        if not output:
            output = word
            continue
        previous = output[-1]
        current = word[0]
        needs_space = not (
            _CJK_RE.match(previous)
            or _CJK_RE.match(current)
            or current in _NO_SPACE_BEFORE
            or previous in _NO_SPACE_AFTER
        )
        output += (" " if needs_space else "") + word
    return output.strip()


def _pixel_bbox_to_points(
    left: int,
    top: int,
    right: int,
    bottom: int,
    *,
    image_width: int,
    image_height: int,
    page_width_pt: float,
    page_height_pt: float,
) -> list[float] | None:
    if image_width <= 0 or image_height <= 0:
        return None
    if page_width_pt <= 0 or page_height_pt <= 0:
        return None
    scale_x = page_width_pt / image_width
    scale_y = page_height_pt / image_height
    x0_pt = left * scale_x
    x1_pt = right * scale_x
    y_top_pt = top * scale_y
    y_bottom_pt = bottom * scale_y
    pdf_top = page_height_pt - y_bottom_pt
    pdf_bottom = page_height_pt - y_top_pt
    return [
        round(min(x0_pt, x1_pt), 2),
        round(min(pdf_top, pdf_bottom), 2),
        round(max(x0_pt, x1_pt), 2),
        round(max(pdf_top, pdf_bottom), 2),
    ]


# --- external OCR helpers -----------------------------------------------


@dataclass
class _ExternalOcrPageOutcome:
    """Outcome of one external OCR call.

    ``lines`` is ``None`` when the call failed; the parser then
    keeps the local tesseract result. ``error`` is a short,
    secret-free category string used for logging and for the
    ``external_status`` field of ``pdf_extraction``.
    """

    lines: list[dict[str, Any]] | None = None
    error: str | None = None


def _describe_provider(provider: Any) -> dict[str, str]:
    """Return a secret-free description of the external provider."""

    if provider is None:
        return {"provider": "", "model": ""}
    name = getattr(provider, "name", "") or ""
    model = ""
    version = ""
    describe = getattr(provider, "describe", None)
    if callable(describe):
        try:
            info = describe() or {}
            if isinstance(info, dict):
                model = str(info.get("model") or "")
                version = str(info.get("version") or "")
                if not name:
                    name = str(info.get("provider") or "")
        except Exception:
            model = ""
    if not name:
        name = type(provider).__name__.lower() or ""
    result = {"provider": str(name), "model": str(model)}
    if version:
        result["version"] = version
    return result


def _sum_line_chars(lines: list[dict[str, Any]]) -> int:
    total = 0
    for line in lines:
        text = line.get("text") if isinstance(line, dict) else None
        if not text:
            continue
        total += sum(1 for ch in text if not ch.isspace())
    return total


def _average_confidence(lines: list[dict[str, Any]]) -> float | None:
    confidences: list[float] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        value = line.get("confidence")
        if value is None:
            continue
        try:
            confidences.append(float(value))
        except (TypeError, ValueError):
            continue
    if not confidences:
        return None
    return sum(confidences) / len(confidences)


def _classify_external_trigger(
    *,
    local_failed: bool,
    local_chars: int,
    local_avg_confidence: float | None,
    min_chars: int,
    confidence_threshold: int,
) -> str:
    """Return a short, secret-free trigger reason for the audit log."""

    if local_failed or local_chars == 0:
        return "no_text"
    if min_chars > 0 and local_chars < min_chars:
        return "low_chars"
    if (
        confidence_threshold > 0
        and local_avg_confidence is not None
        and local_avg_confidence < _confidence_threshold_to_unit(confidence_threshold)
    ):
        return "low_confidence"
    return "manual"


def _confidence_threshold_to_unit(value: int) -> float:
    """Convert the public 0..1000 confidence scale to 0..1."""

    try:
        return max(0.0, min(1.0, float(value) / 1000.0))
    except (TypeError, ValueError):
        return 0.6


def _normalized_bbox_to_points(
    bbox: list[float] | None,
    *,
    page_width_pt: float,
    page_height_pt: float,
) -> list[float] | None:
    """Convert a 0..1000 bbox from the external model into PDF points.

    The external provider is asked to return coordinates in a
    canonical 0..1000 page space (origin top-left) so we do not
    have to re-render the page on its side. This helper maps the
    coordinates back to the actual page size in points; the
    result is rounded to 2 decimal places to match the local
    tesseract converter.
    """

    if not bbox or len(bbox) != 4:
        return None
    if page_width_pt <= 0 or page_height_pt <= 0:
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0 = max(0.0, min(1000.0, x0))
    x1 = max(0.0, min(1000.0, x1))
    y0 = max(0.0, min(1000.0, y0))
    y1 = max(0.0, min(1000.0, y1))
    scale_x = page_width_pt / 1000.0
    scale_y = page_height_pt / 1000.0
    # The provider uses normalized top-left coordinates while the parser's
    # canonical PDF coordinates use a bottom-left origin (as does tesseract).
    pdf_y0 = page_height_pt - y1 * scale_y
    pdf_y1 = page_height_pt - y0 * scale_y
    return [
        round(x0 * scale_x, 2),
        round(min(pdf_y0, pdf_y1), 2),
        round(x1 * scale_x, 2),
        round(max(pdf_y0, pdf_y1), 2),
    ]


def _confidence_to_unit(
    value: Any,
) -> float | None:
    """Normalise a model-reported confidence to the 0..1 range.

    The OpenAI-compatible provider returns either a 0..1000 or
    0..1 confidence. Tests inject either format so the parser
    accepts both and exposes the same ``confidence`` (0..1) on
    the block ``extra`` regardless of source.
    """

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return 0.0
    if number <= 1.0:
        return number
    if number <= 1000.0:
        return number / 1000.0
    return 1.0


def _pick_better_result(
    *,
    local_lines: list[dict[str, Any]],
    external_lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Choose the best of the two per-page OCR results.

    The heuristic prefers the result with more non-whitespace
    characters; when the two are close (within 10%) the local
    result wins because it is deterministic and free. The choice
    is mirrored onto ``Block.extra.source`` so the front-end can
    tell which engine produced the text.
    """

    local_chars = _sum_line_chars(local_lines)
    external_chars = _sum_line_chars(external_lines)
    if external_chars <= 0:
        return local_lines
    if local_chars <= 0:
        return external_lines
    if external_chars > int(local_chars * 1.1):
        return external_lines
    return local_lines


def _record_ocr_outcome(
    *,
    blocks: list[Block],
    heading_path: list[str],
    page_number: int,
    native_block_count: int,
    cleaned_native: str,
    lines: list[dict[str, Any]],
    source: str,
    engine: str,
    provider: str,
    model: str,
    external_used: bool,
    external_status: str,
) -> None:
    """Append the OCR result as ``Block`` entries with the new metadata.

    The block ``extra`` keeps the legacy keys (``source``,
    ``bbox``, ``confidence``, ``ocr_engine``) for back-compat and
    adds the new ones (``engine``, ``provider``, ``model``) so the
    front-end can display "本地识别" vs "外部识别" without parsing
    the OCR engine string. When the external path was tried we
    record ``external_status`` and ``external_used`` so the
    document detail page can show the trigger.
    """

    normalized_native = cleaned_native.casefold()
    for line in lines:
        text = line.get("text") if isinstance(line, dict) else None
        if not text:
            continue
        normalized_line = _strip_whitespace(text).casefold()
        if normalized_native and (
            normalized_line in normalized_native
            or normalized_native in normalized_line
        ):
            continue
        bbox = line.get("bbox") if isinstance(line, dict) else None
        confidence = (
            _confidence_to_unit(line.get("confidence"))
            if isinstance(line, dict)
            else None
        )
        extra: dict[str, Any] = {
            # Keep the historical value so chunking and downstream clients
            # continue to recognize OCR blocks; expose the precise origin
            # separately for the UI and auditing.
            "source": "ocr",
            "ocr_source": source,
            "engine": engine,
            "ocr_engine": engine,
            "bbox": bbox,
            "confidence": confidence,
        }
        if provider:
            extra["provider"] = provider
        if model:
            extra["model"] = model
        # ``external_status`` is mirrored onto every block that
        # came out of an OCR pass so the front-end can tell why a
        # particular page was kept local. When the external model
        # was actually used the status is "completed" / "timeout"
        # / etc.; when the local result was kept the status
        # describes why (skipped because of the per-doc cap,
        # disabled because the operator turned the channel off,
        # or upstream_error because the external call failed).
        extra["external_status"] = external_status
        extra["external_used"] = bool(external_used)
        blocks.append(
            Block(
                type="paragraph",
                text=text,
                heading_path=heading_path.copy(),
                page=page_number,
                paragraph_index=(
                    native_block_count
                    + int(line.get("paragraph_index") or 0)
                ),
                extra=extra,
            )
        )


def _external_lines_to_internal(
    lines: Any,
    *,
    page_width_pt: float,
    page_height_pt: float,
) -> list[dict[str, Any]]:
    """Convert the provider lines to the parser's internal shape.

    The provider returns :class:`ExternalOcrLine` instances (or,
    in tests, raw dicts with the same keys). We turn each into
    ``{"text": str, "confidence": float, "bbox": [x0, y0, x1, y1] | None,
    "paragraph_index": int}`` so the rest of the parser does not
    have to know about the provider types.
    """

    output: list[dict[str, Any]] = []
    if not lines:
        return output
    index = 0
    for raw in lines:
        if hasattr(raw, "text") and hasattr(raw, "bbox"):
            text = getattr(raw, "text", "") or ""
            confidence = getattr(raw, "confidence", None)
            bbox = getattr(raw, "bbox", None)
        elif isinstance(raw, dict):
            text = raw.get("text") or ""
            confidence = raw.get("confidence")
            bbox = raw.get("bbox")
        else:
            continue
        text = " ".join(str(text).split()).strip()
        if not text:
            continue
        normalised_bbox = _normalized_bbox_to_points(
            bbox,
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
        )
        output.append(
            {
                "text": text,
                "confidence": _confidence_to_unit(confidence),
                "bbox": normalised_bbox,
                "paragraph_index": index,
            }
        )
        index += 1
    return output


def _classify_runner_exception(exc: BaseException) -> str:
    """Map an arbitrary runner exception to a short, secret-free category."""

    message = str(exc).lower()
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "auth" in message or "401" in message or "403" in message:
        return "auth"
    if "connect" in message or "network" in message:
        return "network"
    return "upstream"
