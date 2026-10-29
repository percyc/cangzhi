"""Single-frame image OCR reusing the PDF quality, budget and provenance pipeline."""
from io import BytesIO
import warnings

from PIL import Image, ImageOps

from .base import BaseParser, ParserResult
from .pdf import PdfOcrOptions, PdfParser, _PageRender

IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_SIDE = 12_000


class ImageParser(BaseParser):
    def __init__(self, options: PdfOcrOptions | None = None, *, expected_format: str | None = None):
        self.options = (options or PdfOcrOptions(enabled=False)).normalized()
        self.expected_format = expected_format

    def parse(self, content: bytes, content_type: str | None = None) -> ParserResult:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as source:
                    image_format = source.format
                    if image_format not in IMAGE_FORMATS.values() or (
                        self.expected_format and image_format != self.expected_format
                    ):
                        return self._failure("image_format_mismatch", "图片内容与格式不符，请重新导出 PNG、JPEG 或 WebP。")
                    if source.width * source.height > MAX_IMAGE_PIXELS or max(source.size) > MAX_IMAGE_SIDE:
                        return self._failure("image_limit_exceeded", "图片尺寸过大：最多 2500 万像素、单边 12000 像素，请缩小后上传。")
                    if getattr(source, "n_frames", 1) != 1:
                        return self._failure("image_multiframe", "暂不支持动画或多帧图片，请导出为单张静态图片。")
                    exif_orientation = source.getexif().get(274, 1)
                    oriented = ImageOps.exif_transpose(source)
                    # Preserve text on transparent backgrounds. No JPEG round trip for OCR.
                    rgba = oriented.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            return self._failure("image_limit_exceeded", "图片解码尺寸过大，请缩小后上传。")
        except Exception:
            return self._failure("image_invalid", "图片无法解码或已损坏，请重新导出 PNG、JPEG 或 WebP。")

        if not self.options.enabled:
            return self._failure("image_ocr_disabled", "图片需要 OCR，请在设置中启用 OCR 后重新处理。")
        png, pdf = BytesIO(), BytesIO()
        image.save(png, format="PNG")
        # A disposable page descriptor only. Rendering below always uses lossless pixels.
        image.save(pdf, format="PDF", resolution=72.0)
        render = _PageRender(image.width, image.height, float(image.width), float(image.height), png.getvalue())
        result = PdfParser(self.options, renderer_factory=lambda *_: lambda _: render).parse(pdf.getvalue())
        if not result.success or result.structured_content is None:
            return result
        structured = result.structured_content
        if not structured.full_text().strip():
            return ParserResult(success=False,
                error_message="图片未取得可安全入库的文字，请检查清晰度、方向和 OCR 配置；原图已保留。",
                error_details={"reason": "image_ocr_empty", "pdf_extraction": structured.metadata.get("pdf_extraction", {})})
        structured.document_type = "image"
        structured.metadata["image_extraction"] = {
            "version": "image-ocr-v1", "format": image_format,
            "width": image.width, "height": image.height,
            "exif_orientation": exif_orientation,
            "coordinate_source": "exif_transposed_image",
            "coordinate_units": "pixels", "coordinate_origin": "bottom_left",
        }
        return result

    @staticmethod
    def _failure(reason: str, message: str) -> ParserResult:
        return ParserResult(success=False, error_message=message, error_details={"reason": reason})
