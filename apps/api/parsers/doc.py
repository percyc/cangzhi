from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import BaseParser, ParserResult
from .docx import DocxParser


class DocParser(BaseParser):
    """Convert legacy Word binary documents to DOCX before parsing."""

    conversion_timeout_seconds = 60

    def parse(self, content: bytes, content_type: str | None = None) -> ParserResult:
        executable = shutil.which("soffice") or shutil.which("libreoffice")
        if executable is None:
            return ParserResult(
                success=False,
                error_message="DOC 解析组件不可用，请联系管理员安装 LibreOffice",
                error_details={"reason": "doc_converter_unavailable"},
            )

        try:
            with tempfile.TemporaryDirectory(prefix="cangzhi-doc-") as directory:
                workdir = Path(directory)
                source_path = workdir / "source.doc"
                output_path = workdir / "source.docx"
                profile_path = workdir / "libreoffice-profile"
                source_path.write_bytes(content)

                completed = subprocess.run(
                    [
                        executable,
                        f"-env:UserInstallation={profile_path.as_uri()}",
                        "--headless",
                        "--nologo",
                        "--nodefault",
                        "--nolockcheck",
                        "--norestore",
                        "--convert-to",
                        "docx:Office Open XML Text",
                        "--outdir",
                        str(workdir),
                        str(source_path),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=self.conversion_timeout_seconds,
                )
                if completed.returncode != 0 or not output_path.is_file():
                    details = (
                        completed.stderr.decode("utf-8", errors="replace")
                        or completed.stdout.decode("utf-8", errors="replace")
                    ).strip()
                    return ParserResult(
                        success=False,
                        error_message="DOC 文件转换失败，文件可能已损坏或受密码保护",
                        error_details={
                            "reason": "doc_conversion_failed",
                            "converter_output": details[-2000:],
                        },
                    )

                result = DocxParser().parse(
                    output_path.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
                if result.success and result.structured_content is not None:
                    result.structured_content.document_type = "doc"
                    result.structured_content.metadata.update(
                        {
                            "source_format": "doc",
                            "converted_format": "docx",
                        }
                    )
                return result
        except subprocess.TimeoutExpired:
            return ParserResult(
                success=False,
                error_message="DOC 文件转换超时，文件可能过大或已损坏",
                error_details={"reason": "doc_conversion_timeout"},
            )
        except OSError as exc:
            return ParserResult(
                success=False,
                error_message=f"DOC 文件转换失败: {exc}",
                error_details={
                    "reason": "doc_conversion_error",
                    "exception": str(exc),
                },
            )

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        if (content_type or "").lower() in {
            "application/msword",
            "application/doc",
            "application/vnd.ms-word",
            "application/vnd.msword",
            "application/winword",
        }:
            return True
        return bool(file_name and file_name.lower().endswith(".doc"))
