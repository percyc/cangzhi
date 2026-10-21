from __future__ import annotations

from .base import ParserResult
from .doc import DocParser

WPS_MIMES = frozenset({
    "application/kswps", "application/wps", "application/x-wps",
    "application/vnd.ms-works", "application/vnd.wps-office.wps",
})


class WpsParser(DocParser):
    """Convert compatible WPS Writer containers, never decode unknown binary text."""

    source_format = "wps"

    def conversion_arguments(self, content: bytes) -> list[str]:
        # The .wps extension is shared with Works. Select the intended compatible
        # container filter explicitly rather than relying on the suffix alone.
        name = "Office Open XML Text" if content.startswith(b"PK\x03\x04") else "MS Word 97"
        return [f"--infilter={name}"]

    def parse(self, content: bytes, content_type: str | None = None) -> ParserResult:
        # Unknown historic WPS/Works formats must not be imported by Writer's
        # plain-text fallback. Container acceptance is not a validity guarantee.
        if not content.startswith((b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"PK\x03\x04")):
            return ParserResult(False, error_message=(
                "无法识别此 WPS 文字格式，请使用 WPS 将原件另存为 DOCX 或 PDF 后上传"
            ), error_details={"reason": "wps_unsupported_container"})
        result = super().parse(content, content_type)
        if not result.success:
            reason = (result.error_details or {}).get("reason", "doc_conversion_failed")
            # Converter diagnostics may contain source text; do not persist them.
            result.error_details = {"reason": reason.replace("doc_", "wps_", 1)}
            result.error_message = (
                "WPS 解析需要 LibreOffice，请联系管理员检查转换组件"
                if reason == "doc_converter_unavailable" else
                "WPS 文字转换失败或超时，可能是旧版私有格式、加密或文件损坏；"
                "请另存为 DOCX 或 PDF 后上传"
            )
        return result

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        return bool(file_name and file_name.lower().endswith(".wps")) or (
            (content_type or "").lower().split(";", 1)[0].strip() in WPS_MIMES
        )
