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

from .base import BaseParser, Block, ParserResult, StructuredContent

logger = logging.getLogger(__name__)

PDF_OCR_VERSION = "pdf-hybrid-v1"
PDF_OCR_ENGINE = "tesseract"


@dataclass
class PdfOcrOptions:
    """Optional OCR fallback for PDF pages without native selectable text.

    Defaults match the worker's runtime configuration. ``enabled`` gates the
    whole pipeline so unit tests and integrations can run without tesseract or
    pypdfium2 installed.
    """

    enabled: bool = True
    language: str = "chi_sim+eng"
    dpi: int = 180
    min_native_chars: int = 24
    max_pages: int = 300
    timeout_seconds: float = 30.0

    def normalized(self) -> PdfOcrOptions:
        return PdfOcrOptions(
            enabled=bool(self.enabled),
            language=(self.language or "chi_sim+eng").strip() or "chi_sim+eng",
            dpi=_clamp_int(self.dpi, 72, 600, 180),
            min_native_chars=_clamp_int(self.min_native_chars, 1, 10_000, 24),
            max_pages=_clamp_int(self.max_pages, 1, 50_000, 300),
            timeout_seconds=_clamp_float(self.timeout_seconds, 1.0, 600.0, 30.0),
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
    ) -> None:
        # OCR is enabled explicitly by the worker. Keeping a bare parser
        # native-only preserves its lightweight use in API/tests/integrations.
        self._options = (options or PdfOcrOptions(enabled=False)).normalized()
        self._renderer_factory = renderer_factory
        self._tesseract_runner = tesseract_runner
        self._tesseract_lookup = tesseract_lookup

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

        def _record_skip(reason: str, page_number: int) -> None:
            bucket = ocr_skipped_reasons.setdefault(reason, [])
            bucket.append(page_number)

        def _tesseract_path() -> str | None:
            if self._tesseract_lookup is not None:
                return self._tesseract_lookup()
            return shutil.which("tesseract")

        ocr_renderer: Callable[[int], _PageRender] | None = None
        renderer_initialized = False
        ocr_processed = 0

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
            result = self._ocr_page(
                page_number,
                options,
                ocr_renderer,
            )
            ocr_processed += 1
            if result.error is not None or not result.lines:
                ocr_failed_pages.append(page_number)
                _record_skip(result.error or "ocr_no_text", page_number)
                continue
            ocr_completed_pages.append(page_number)
            for line in result.lines:
                normalized_ocr = _strip_whitespace(line["text"]).casefold()
                normalized_native = cleaned_native.casefold()
                if normalized_native and (
                    normalized_ocr in normalized_native
                    or normalized_native in normalized_ocr
                ):
                    continue
                blocks.append(
                    Block(
                        type="paragraph",
                        text=line["text"],
                        heading_path=heading_path.copy(),
                        page=page_number,
                        paragraph_index=(
                            native_block_count + line["paragraph_index"]
                        ),
                        extra={
                            "source": "ocr",
                            "bbox": line.get("bbox"),
                            "confidence": line.get("confidence"),
                            "ocr_engine": PDF_OCR_ENGINE,
                        },
                    )
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
        is_heading = (
            (len(line) < 100 and line.isupper())
            or line.startswith(("Chapter", "Section"))
        )
        if is_heading:
            level = 1 if len(line) < 30 else 2
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
