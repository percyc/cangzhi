"""Excel workbook parsers that preserve sheet and row boundaries."""

from __future__ import annotations

import math
import re
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, time
from io import BytesIO
from itertools import zip_longest
from typing import Any

from .base import BaseParser, Block, ParserResult, StructuredContent

MAX_ARCHIVE_ENTRIES = 10_000
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_SHEETS = 100
MAX_ROWS_PER_SHEET = 150_000
MAX_CELLS_TOTAL = 2_000_000
MAX_REGIONS_PER_SHEET = 1_000
MAX_CELL_CHARS = 10_000
MAX_TABLE_BLOCK_CHARS = 12_000
MAX_ROWS_PER_BLOCK = 200

_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_HEADER_HINT_RE = re.compile(
    r"(?:编号|序号|代码|名称|姓名|日期|时间|金额|数量|单价|合计|状态|类型|"
    r"部门|地区|城市|客户|供应商|备注|说明|地址|电话|邮箱|id|name|date|"
    r"time|amount|price|count|status|type|total|description)$",
    re.IGNORECASE,
)


class SpreadsheetLimitError(ValueError):
    pass


def _format_cell(value: object, *, number_format: str | None = None) -> str:
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
        display = _format_number_with_excel_format(value, number_format)
        if display is not None:
            return display
        if value.is_integer():
            return str(int(value))
    if isinstance(value, int) and not isinstance(value, bool):
        display = _format_number_with_excel_format(value, number_format)
        if display is not None:
            return display
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    # A cell must remain one TSV field; embedded newlines are still readable.
    text = text.replace("\t", " ").replace("\n", " ↵ ")
    return text[:MAX_CELL_CHARS]


def _format_number_with_excel_format(
    value: float,
    number_format: str | None,
) -> str | None:
    """Preserve common display semantics without trying to emulate Excel fully."""
    code = (number_format or "").split(";", 1)[0].strip()
    if not code or code.lower() == "general":
        return None
    if "%" in code:
        match = re.search(r"0(?:\.(0+))?%", code)
        decimals = len(match.group(1) or "") if match else 0
        return f"{float(value) * 100:.{decimals}f}%"
    if re.fullmatch(r"0+", code) and float(value).is_integer():
        return f"{int(value):0{len(code)}d}"
    symbol = next((item for item in ("￥", "¥", "$") if item in code), None)
    if symbol:
        decimals_match = re.search(r"\.(0+)", code)
        decimals = len(decimals_match.group(1)) if decimals_match else 0
        return f"{symbol}{float(value):,.{decimals}f}"
    return None


def _trim_row(values: Sequence[object]) -> list[str]:
    cells = [_format_cell(value) for value in values]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _excel_column_name(index: int) -> str:
    result = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _looks_like_header(
    rows: list[tuple[int, list[str]]],
) -> bool:
    if len(rows) < 2:
        return False
    first = rows[0][1]
    non_empty = [cell.strip() for cell in first if cell.strip()]
    if len(non_empty) < 2 or len(set(non_empty)) != len(non_empty):
        return False
    if any(len(cell) > 40 or cell.startswith("=") for cell in non_empty):
        return False
    numeric_first = sum(bool(_NUMBER_RE.fullmatch(cell)) for cell in non_empty)
    if numeric_first:
        return False
    if any(_HEADER_HINT_RE.search(cell) for cell in non_empty):
        return True
    second = [cell.strip() for cell in rows[1][1] if cell.strip()]
    numeric_second = sum(bool(_NUMBER_RE.fullmatch(cell)) for cell in second)
    return numeric_second > 0 and all(len(cell) <= 24 for cell in non_empty)


def _column_names(
    rows: list[tuple[int, list[str]]],
) -> tuple[list[str], int | None]:
    width = max((len(cells) for _, cells in rows), default=0)
    header_row = rows[0][0] if _looks_like_header(rows) else None
    source = rows[0][1] if header_row is not None else []
    names: list[str] = []
    seen: dict[str, int] = {}
    for index in range(width):
        candidate = source[index].strip() if index < len(source) else ""
        candidate = candidate or f"{_excel_column_name(index)}列"
        count = seen.get(candidate, 0) + 1
        seen[candidate] = count
        names.append(candidate if count == 1 else f"{candidate}_{count}")
    return names, header_row


def _split_regions(
    rows: list[tuple[int, list[str]]],
) -> list[list[tuple[int, list[str]]]]:
    regions: list[list[tuple[int, list[str]]]] = []
    current: list[tuple[int, list[str]]] = []
    for row_number, cells in rows:
        if any(cells):
            current.append((row_number, cells))
        elif current:
            regions.append(current)
            current = []
    if current:
        regions.append(current)
    return regions


def _semantic_row(
    row_number: int,
    cells: list[str],
    columns: list[str],
) -> str:
    values = [
        f"{columns[index] if index < len(columns) else _excel_column_name(index) + '列'}="
        f"{'公式' if value.startswith('=') else ''}{value}"
        for index, value in enumerate(cells)
        if value
    ]
    return f"行 {row_number}｜" + "｜".join(values)


def _sheet_blocks(
    sheet_name: str,
    rows: Iterable[Sequence[object]],
    *,
    paragraph_offset: int,
) -> tuple[list[Block], dict[str, Any], int]:
    blocks: list[Block] = []
    non_empty_rows = 0
    non_empty_cells = 0
    formula_cells = 0
    visited_cells = 0

    collected_rows: list[tuple[int, list[str]]] = []
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
        collected_rows.append((row_number, cells))
        if not cells or not any(cells):
            continue
        non_empty_rows += 1
        non_empty_cells += sum(bool(cell) for cell in cells)
        formula_cells += sum(cell.startswith("=") for cell in cells)

    regions = _split_regions(collected_rows)
    if len(regions) > MAX_REGIONS_PER_SHEET:
        raise SpreadsheetLimitError(
            f"工作表“{sheet_name}”超过 {MAX_REGIONS_PER_SHEET} 个数据区域限制"
        )
    region_metadata: list[dict[str, Any]] = []
    for region_index, region in enumerate(regions, start=1):
        columns, header_row = _column_names(region)
        data_rows = [item for item in region if item[0] != header_row]
        region_metadata.append(
            {
                "region_index": region_index,
                "row_start": region[0][0],
                "row_end": region[-1][0],
                "header_row": header_row,
                "column_names": columns,
            }
        )
        if not data_rows:
            continue
        buffered: list[tuple[int, str]] = []
        buffered_chars = 0

        def flush(
            *,
            current_region_index: int = region_index,
            current_header_row: int | None = header_row,
            current_columns: list[str] = columns,
        ) -> None:
            nonlocal buffered, buffered_chars
            if not buffered:
                return
            row_start = buffered[0][0]
            row_end = buffered[-1][0]
            blocks.append(
                Block(
                    type="table",
                    text="\n".join(line for _, line in buffered),
                    heading_path=[sheet_name, f"数据区域 {current_region_index}"],
                    paragraph_index=paragraph_offset + row_start - 1,
                    extra={
                        "sheet_name": sheet_name,
                        "region_index": current_region_index,
                        "row_start": row_start,
                        "row_end": row_end,
                        "header_row": current_header_row,
                        "column_names": current_columns,
                    },
                )
            )
            buffered = []
            buffered_chars = 0

        for row_number, cells in data_rows:
            line = _semantic_row(row_number, cells, columns)
            if buffered and (
                len(buffered) >= MAX_ROWS_PER_BLOCK
                or buffered_chars + len(line) + 1 > MAX_TABLE_BLOCK_CHARS
            ):
                flush()
            buffered.append((row_number, line))
            buffered_chars += len(line) + 1
        flush()

    if regions:
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
            "data_region_count": len(regions),
            "regions": region_metadata,
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
                "data_region_count": 0,
            }
            workbook_regions: list[dict[str, Any]] = []
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
                workbook_regions.extend(
                    {"sheet_name": sheet.title, **region}
                    for region in sheet_totals["regions"]
                )
            workbook.close()
            values_workbook.close()
            return ParserResult(
                success=True,
                structured_content=StructuredContent(
                    document_type="xlsx",
                    blocks=blocks,
                    metadata={
                        "source_format": "xlsx",
                        "spreadsheet_schema_version": 2,
                        "sheet_count": len(sheet_names),
                        "sheet_names": sheet_names,
                        "regions": workbook_regions,
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
        except Exception as exc:  # noqa: BLE001 - parser failures become safe results
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
            number_format = getattr(formula_cell, "number_format", None)
            if isinstance(formula, str) and formula.startswith("="):
                cached_display = _xlsx_display_value(cached, number_format)
                combined.append(
                    f"{formula} ⇒ {cached_display}"
                    if cached is not None
                    else formula
                )
            else:
                combined.append(
                    _xlsx_display_value(formula, number_format)
                    if formula is not None
                    else ""
                )
        yield combined


def _xlsx_display_value(value: object, number_format: str | None) -> str:
    display = _format_cell(value, number_format=number_format)
    raw = _format_cell(value)
    if display and raw and display != raw:
        return f"{display}（原始值：{raw}）"
    return display


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
            totals = {
                "non_empty_rows": 0,
                "non_empty_cells": 0,
                "formula_cells": 0,
                "data_region_count": 0,
            }
            workbook_regions: list[dict[str, Any]] = []
            visited_cells = 0
            for sheet in workbook.sheets():
                sheet_blocks, sheet_totals, sheet_cells = _sheet_blocks(
                    sheet.name,
                    _xls_rows(sheet, workbook),
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
                workbook_regions.extend(
                    {"sheet_name": sheet.name, **region}
                    for region in sheet_totals["regions"]
                )
            sheet_names = list(workbook.sheet_names())
            workbook.release_resources()
            return ParserResult(
                success=True,
                structured_content=StructuredContent(
                    document_type="xls",
                    blocks=blocks,
                    metadata={
                        "source_format": "xls",
                        "spreadsheet_schema_version": 2,
                        "sheet_count": len(sheet_names),
                        "sheet_names": sheet_names,
                        "regions": workbook_regions,
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
        except Exception as exc:  # noqa: BLE001 - parser failures become safe results
            return ParserResult(
                success=False,
                error_message=f"Excel 解析失败：{exc}",
                error_details={"reason": "xls_parse_failed"},
            )


def _xls_rows(sheet: object, workbook: object) -> Iterator[list[object]]:
    for row_index in range(sheet.nrows):
        yield [
            _xls_value(sheet.cell(row_index, column), workbook)
            for column in range(sheet.ncols)
        ]


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
