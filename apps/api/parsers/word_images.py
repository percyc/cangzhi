"""Bounded embedded-image OCR for mixed Word documents; never fetch links."""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import time
import warnings

from PIL import Image

from .base import Block
from .image import ImageParser, IMAGE_FORMATS, MAX_IMAGE_PIXELS, MAX_IMAGE_SIDE
from .pdf import PdfOcrOptions

MAX_UNIQUE_IMAGES = 32
MAX_OCCURRENCES = 128
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
START_BUDGET_SECONDS = 180


def _crop_spec(element):
    from docx.oxml.ns import qn
    containers = [parent for parent in element.iterancestors() if parent.tag == qn("pic:pic")]
    rects = [rect for parent in containers for rect in parent.iter(qn("a:srcRect"))]
    if len(rects) > 1:
        raise ValueError("ambiguous crop")
    if rects:
        values = [float(rects[0].get(key, "0")) / 100000 for key in ("l", "t", "r", "b")]
    else:
        def fraction(value):
            return float(value[:-1])/65536 if value.endswith("f") else float(value[:-1])/100 if value.endswith("%") else float(value)
        values = [fraction(element.get(key, "0")) for key in ("cropleft", "croptop", "cropright", "cropbottom")]
    if any(not -1 <= value < 1 for value in values):
        raise ValueError("invalid crop")
    # Negative crop is empty padding outside the original, not hidden content.
    values = tuple(max(0, value) for value in values)
    if values[0] + values[2] >= 1 or values[1] + values[3] >= 1:
        raise ValueError("empty crop")
    return values


def _visible_blob(blob, crop):
    if not any(crop):
        return blob
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(blob)) as image:
            if (image.format not in IMAGE_FORMATS.values() or getattr(image, "n_frames", 1) != 1
                    or image.width * image.height > MAX_IMAGE_PIXELS or max(image.size) > MAX_IMAGE_SIDE
                    or image.getexif().get(274, 1) != 1):
                raise ValueError("unsupported cropped image")
            # Round inward so cropped-out pixels never reach local/external OCR.
            import math
            left, top, right, bottom = crop
            box = (math.ceil(left*image.width), math.ceil(top*image.height),
                   math.floor((1-right)*image.width), math.floor((1-bottom)*image.height))
            if box[0] >= box[2] or box[1] >= box[3]:
                raise ValueError("empty crop")
            output = BytesIO()
            image.crop(box).save(output, "PNG")
            return output.getvalue()


class _BudgetProvider:
    def __init__(self, provider, limit):
        self.provider, self.limit, self.calls = provider, limit, 0
        self.name = getattr(provider, "name", "external")

    def describe(self):
        return self.provider.describe()

    def recognize(self, *args, **kwargs):
        # Charge failures too; a new image must not reset the document budget.
        if self.calls >= self.limit:
            raise RuntimeError("word_image_budget_exhausted")
        self.calls += 1
        return self.provider.recognize(*args, **kwargs)


def add_embedded_image_text(doc, structured, options):
    """Append OCR after the containing body paragraph/table, not a fake PDF page.

    Native blocks are unchanged. Image-only Word keeps its existing PDF fallback.
    Binary bytes and candidate bodies never enter diagnostics.
    """
    if not structured.full_text().strip():
        return
    from docx.oxml.ns import qn
    options = options if isinstance(options, PdfOcrOptions) else PdfOcrOptions(**dict(options))
    options = options.normalized()
    provider = _BudgetProvider(options.external_provider, options.external_max_pages) if options.external_provider else None
    summary = {"version": "word-images-v1", "items": [], "external_calls": 0,
               "placement": "after_containing_body_block", "status": "completed"}
    cache, additions = {}, {}
    total_bytes = total_chars = emitted_chars = occurrences = 0
    deadline = time.monotonic() + START_BUDGET_SECONDS
    native_by_index = {}
    for block in structured.blocks:
        native_by_index.setdefault(block.paragraph_index, []).append(block)
    body_index = -1
    heading_path = []
    for child in doc.element.body.iterchildren():
        if child.tag not in {qn("w:p"), qn("w:tbl")}:
            continue
        body_index += 1
        matching = native_by_index.get(body_index, [])
        if matching:
            heading_path = matching[-1].heading_path
        for element in child.iter():
            if element.tag not in {qn("a:blip"), "{urn:schemas-microsoft-com:vml}imagedata"}:
                continue
            occurrences += 1
            if occurrences > MAX_OCCURRENCES:
                summary["occurrence_limit_reached"] = True
                break
            item = {"image_index": occurrences, "body_index": body_index}
            summary["items"].append(item)
            ancestors = list(element.iterancestors())
            unsupported = {qn("w:" + tag) for tag in ("del", "ins", "moveFrom", "moveTo", "sdt", "customXml", "txbxContent")}
            hidden = any(flag.get(qn("w:val"), "1") not in {"0", "false", "off"}
                         for parent in ancestors if parent.tag == qn("w:r")
                         for flag in parent.iter() if flag.tag in {qn("w:vanish"), qn("w:webHidden")})
            if hidden or any(parent.tag in unsupported for parent in ancestors):
                item["status"] = "unsupported_structure"
                continue
            try:
                crop = _crop_spec(element)
            except (ValueError, TypeError, OverflowError):
                item["status"] = "cropped_image"
                continue
            rid = element.get(qn("r:embed")) or element.get(qn("r:id"))
            rel = doc.part.rels.get(rid) if rid else None
            if rel is None or rel.is_external:
                item["status"] = "linked_or_missing"
                continue
            part = rel.target_part
            if not part.content_type.startswith("image/"):
                item["status"] = "unsupported"
                continue
            blob = part.blob
            if len(blob) > MAX_IMAGE_BYTES:
                item["status"] = "image_limit"
                continue
            digest = sha256(blob + repr(crop).encode("ascii")).hexdigest()
            item["image_hash"] = digest
            if digest not in cache:
                if not options.enabled:
                    item["status"] = "disabled"
                    continue
                if len(cache) >= min(MAX_UNIQUE_IMAGES, options.max_pages) or total_bytes + len(blob) > MAX_TOTAL_BYTES or time.monotonic() >= deadline:
                    item["status"] = "budget_exhausted"
                    continue
                total_bytes += len(blob)
                active_provider = provider if provider and provider.calls < provider.limit else None
                image_options = replace(options, external_provider=active_provider,
                                        external_max_pages=1 if active_provider else 0)
                try:
                    result = ImageParser(image_options).parse(_visible_blob(blob, crop))
                    data = result.structured_content
                    if not result.success or data is None:
                        reason = (result.error_details or {}).get("reason")
                        extraction = (result.error_details or {}).get("pdf_extraction", {})
                        status = ("unsupported" if reason in {"image_format_mismatch", "image_multiframe"}
                                  else "low_quality" if extraction.get("quarantined_pages") else "failed")
                        cache[digest] = ([], status, {})
                    elif total_chars + len(data.full_text()) > MAX_TEXT_CHARS:
                        cache[digest] = ([], "budget_exhausted", {})
                    else:
                        total_chars += len(data.full_text())
                        extraction = data.metadata.get("pdf_extraction", {})
                        status = "review" if extraction.get("ocr_status") != "completed" else "completed"
                        cache[digest] = (data.blocks, status, data.metadata.get("image_extraction", {}))
                except Exception:
                    # Per-image failure must never discard the native body.
                    cache[digest] = ([], "failed", {})
            else:
                item["reused"] = True
            image_blocks, item["status"], geometry = cache[digest]
            item["sources"] = sorted({b.extra.get("ocr_source", "unknown") for b in image_blocks})
            item["characters"] = sum(len(b.text) for b in image_blocks)
            if emitted_chars + item["characters"] > MAX_TEXT_CHARS:
                item["status"] = "budget_exhausted"
                item["characters"] = 0
                continue
            emitted_chars += item["characters"]
            for block in image_blocks:
                additions.setdefault(body_index, []).append(Block(
                    type="paragraph", text=block.text, heading_path=list(heading_path),
                    paragraph_index=body_index, page=None,
                    extra={**block.extra, "docx_body_index": body_index,
                           "word_image_index": occurrences, "image_hash": digest,
                           "coordinate_source": "embedded_visible_image", "coordinate_units": "pixels",
                           "image_crop": list(crop),
                           "coordinate_origin": "bottom_left", "image_width": geometry.get("width"),
                           "image_height": geometry.get("height"), "placement": summary["placement"]},
                ))
        if summary.get("occurrence_limit_reached"):
            break
    if not summary["items"]:
        return
    summary["external_calls"] = provider.calls if provider else 0
    if summary.get("occurrence_limit_reached") or any(item["status"] != "completed" for item in summary["items"]):
        summary["status"] = "partial"
    structured.blocks = [block for index in sorted(set(native_by_index) | set(additions))
                         for block in native_by_index.get(index, []) + additions.get(index, [])]
    structured.metadata["word_embedded_ocr"] = summary
