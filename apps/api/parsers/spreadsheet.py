"""Excel workbook parsers that preserve sheet and row boundaries."""

from __future__ import annotations

import math
import zipfile
from datetime import date, datetime, time
from io import BytesIO
from itertools import zip_longest
from typing import Iterable, Iterator, Sequence

from .base import BaseParser, Block, ParserResult, StructuredContent

MAX_ARCHIVE_ENTRIES = 10_000
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_SHEETS = 100
MAX_ROWS_PER_SHEET = 100_000
MAX_CELLS_TOTAL = 2_000_000
MAX_CELL_CHARS = 10_000
MAX_TABLE_BLOCK_CHARS = 12_000
MAX_ROWS_PER_BLOCK = 200


class SpreadsheetLimitError(ValueError):
    pass


def _format_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    # A cell must remain one TSV field; embedded newlines are still readable.
    text = text.replace("\t", " ").replace("\n", " ↵ ")
    return text[:MAX_CELL_CHARS]


def _trim_row(values: Sequence[object]) -> list[str]:
    cells = [_format_cell(value) for value in values]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _sheet_blocks(
    sheet_name: str,
    rows: Iterable[Sequence[object]],
    *,
    paragraph_offset: int,
) -> tuple[list[Block], dict[str, int], int]:
    blocks: list[Block] = []
    buffered: list[str] = []
    buffered_chars = 0
    block_start_row = 1
    non_empty_rows = 0
    non_empty_cells = 0
    formula_cells = 0
    visited_cells = 0

    def flush() -> None:
        nonlocal buffered, buffered_chars
        if not buffered:
            return
        blocks.append(
            Block(
                type="table",
                text="\n".join(buffered),
                heading_path=[sheet_name],
                paragraph_index=paragraph_offset + block_start_row - 1,
            )
        )
        buffered = []
        buffered_chars = 0

    for row_number, values in enumerate(rows, start=1):
        if row_number > MAX_ROWS_PER_SHEET:
            raise SpreadsheetLimitError(
                f"工作表“{sheet_name}”超过 {MAX_ROWS_PER_SHEET} 行限制"
            )
        visited_cells += len(values)
        if visited_cells > MAX_CELLS_TOTAL:
            raise SpreadsheetLimitError(
                f"工作表“{sheet_name}”超过 {MAX_CELLS_TOTAL} 个单元格限制"
            )
        cells = _trim_row(values)
        if not cells or not any(cells):
            continue
        line = "\t".join(cells)
        if buffered and (
            len(buffered) >= MAX_ROWS_PER_BLOCK
            or buffered_chars + len(line) + 1 > MAX_TABLE_BLOCK_CHARS
        ):
            flush()
        if not buffered:
            block_start_row = row_number
        buffered.append(line)
        buffered_chars += len(line) + 1
        non_empty_rows += 1
        non_empty_cells += sum(bool(cell) for cell in cells)
        formula_cells += sum(cell.startswith("=") for cell in cells)

    flush()
    if blocks:
        blocks.insert(
            0,
            Block(
                type="heading",
                text=sheet_name,
                heading_path=[sheet_name],
                paragraph_index=paragraph_offset,
                level=1,
            ),
        )
    return (
        blocks,
        {
            "non_empty_rows": non_empty_rows,
            "non_empty_cells": non_empty_cells,
            "formula_cells": formula_cells,
        },
        visited_cells,
    )


def _validate_xlsx_archive(content: bytes) -> None:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise SpreadsheetLimitError("Excel 压缩包包含过多文件")
            if sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_BYTES:
                raise SpreadsheetLimitError("Excel 解压后内容过大")
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效的 XLSX 工作簿") from exc


class XlsxParser(BaseParser):
    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        return bool(
            (file_name or "").lower().endswith(".xlsx")
            or "spreadsheetml.sheet" in (content_type or "").lower()
        )

    def parse(self, content: bytes | str, content_type: str | None = None) -> ParserResult:
        if isinstance(content, str):
            content = content.encode()
        try:
            _validate_xlsx_archive(content)
            from openpyxl import load_workbook

            workbook = load_workbook(
                BytesIO(content),
                read_only=True,
                data_only=False,
                keep_links=False,
            )
            values_workbook = load_workbook(
                BytesIO(content),
                read_only=True,
                data_only=True,
                keep_links=False,
            )
            if len(workbook.sheetnames) > MAX_SHEETS:
                raise SpreadsheetLimitError(
                    f"工作簿超过 {MAX_SHEETS} 个工作表限制"
                )
            blocks: list[Block] = []
            totals = {
                "non_empty_rows": 0,
                "non_empty_cells": 0,
                "formula_cells": 0,
            }
            visited_cells = 0
            sheet_names = list(workbook.sheetnames)
            for sheet, values_sheet in zip(
                workbook.worksheets,
                values_workbook.worksheets,
            ):
                sheet_blocks, sheet_totals, sheet_cells = _sheet_blocks(
                    sheet.title,
                    _xlsx_rows(sheet, values_sheet),
                    paragraph_offset=visited_cells,
                )
                visited_cells += sheet_cells
                if visited_cells > MAX_CELLS_TOTAL:
                    raise SpreadsheetLimitError(
                        f"工作簿超过 {MAX_CELLS_TOTAL} 个单元格限制"
                    )
                blocks.extend(sheet_blocks)
                for key in totals:
                    totals[key] += sheet_totals[key]
            workbook.close()
            values_workbook.close()
            return ParserResult(
                success=True,
                structured_content=StructuredContent(
                    document_type="xlsx",
                    blocks=blocks,
                    metadata={
                        "source_format": "xlsx",
                        "sheet_count": len(sheet_names),
                        "sheet_names": sheet_names,
                        **totals,
                    },
                ),
            )
        except SpreadsheetLimitError as exc:
            return ParserResult(
                success=False,
                error_message=str(exc),
                error_details={"reason": "spreadsheet_limit_exceeded"},
            )
        except Exception as exc:
            return ParserResult(
                success=False,
                error_message=f"Excel 解析失败：{exc}",
                error_details={"reason": "xlsx_parse_failed"},
            )


def _xlsx_rows(sheet: object, values_sheet: object) -> Iterator[list[object]]:
    """Yield formulas and include cached results when the producer stored them."""
    formula_rows = sheet.iter_rows()
    value_rows = values_sheet.iter_rows()
    for formula_row, value_row in zip_longest(
        formula_rows,
        value_rows,
        fillvalue=(),
    ):
        combined: list[object] = []
        for formula_cell, value_cell in zip_longest(
            formula_row,
            value_row,
            fillvalue=None,
        ):
            formula = getattr(formula_cell, "value", None)
            cached = getattr(value_cell, "value", None)
            if isinstance(formula, str) and formula.startswith("="):
                combined.append(
                    f"{formula} ⇒ {_format_cell(cached)}"
                    if cached is not None
                    else formula
                )
            else:
                combined.append(formula)
        yield combined


class XlsParser(BaseParser):
    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        return bool(
            (file_name or "").lower().endswith(".xls")
            or (content_type or "").lower()
            in {
                "application/vnd.ms-excel",
                "application/excel",
                "application/x-excel",
                "application/x-msexcel",
            }
        )

    def parse(self, content: bytes | str, content_type: str | None = None) -> ParserResult:
        if isinstance(content, str):
            content = content.encode()
        try:
            import xlrd

            workbook = xlrd.open_workbook(
                file_contents=content,
                on_demand=True,
            )
            if workbook.nsheets > MAX_SHEETS:
                raise SpreadsheetLimitError(
                    f"工作簿超过 {MAX_SHEETS} 个工作表限制"
                )
            blocks: list[Block] = []
            totals = {"non_empty_rows": 0, "non_empty_cells": 0, "formula_cells": 0}
            visited_cells = 0
            for sheet in workbook.sheets():
                def rows() -> Iterator[list[object]]:
                    for row_index in range(sheet.nrows):
                        yield [
                            _xls_value(sheet.cell(row_index, column), workbook)
                            for column in range(sheet.ncols)
                        ]

                sheet_blocks, sheet_totals, sheet_cells = _sheet_blocks(
                    sheet.name,
                    rows(),
                    paragraph_offset=visited_cells,
                )
                visited_cells += sheet_cells
                if visited_cells > MAX_CELLS_TOTAL:
                    raise SpreadsheetLimitError(
                        f"工作簿超过 {MAX_CELLS_TOTAL} 个单元格限制"
                    )
                blocks.extend(sheet_blocks)
                for key in totals:
                    totals[key] += sheet_totals[key]
            sheet_names = list(workbook.sheet_names())
            workbook.release_resources()
            return ParserResult(
                success=True,
                structured_content=StructuredContent(
                    document_type="xls",
                    blocks=blocks,
                    metadata={
                        "source_format": "xls",
                        "sheet_count": len(sheet_names),
                        "sheet_names": sheet_names,
                        **totals,
                    },
                ),
            )
        except SpreadsheetLimitError as exc:
            return ParserResult(
                success=False,
                error_message=str(exc),
                error_details={"reason": "spreadsheet_limit_exceeded"},
            )
        except Exception as exc:
            return ParserResult(
                success=False,
                error_message=f"Excel 解析失败：{exc}",
                error_details={"reason": "xls_parse_failed"},
            )


def _xls_value(cell: object, workbook: object) -> object:
    """Convert xlrd date cells while leaving all other cached values intact."""
    try:
        import xlrd

        if cell.ctype == xlrd.XL_CELL_DATE:
            return xlrd.xldate.xldate_as_datetime(cell.value, workbook.datemode)
        if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
            return None
        if cell.ctype == xlrd.XL_CELL_BOOLEAN:
            return bool(cell.value)
        if cell.ctype == xlrd.XL_CELL_ERROR:
            return xlrd.error_text_from_code.get(cell.value, f"#ERROR({cell.value})")
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return cell.value
