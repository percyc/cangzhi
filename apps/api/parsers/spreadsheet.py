"""Excel workbook parsers that preserve sheet and row boundaries."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, time
from io import BytesIO
from itertools import zip_longest
from typing import Any

from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries

from .base import BaseParser, Block, ParserResult, StructuredContent
from .spreadsheet_layout import region_risks

MAX_ARCHIVE_ENTRIES = 10_000
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_SHEETS = 100
MAX_ROWS_PER_SHEET = 150_000
MAX_CELLS_TOTAL = 2_000_000
MAX_TEXT_CHARS_TOTAL = 32_000_000
MAX_REGIONS_PER_SHEET = 1_000
MAX_CELL_CHARS = 10_000
MAX_TABLE_BLOCK_CHARS = 12_000
MAX_ROWS_PER_BLOCK = 200
MAX_MERGE_RANGES = 10_000

# The XML namespace for sheet data; openpyxl emits this exact URI.
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

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
    merged_ranges: Sequence[tuple[int, int, int, int]] = (),
) -> tuple[list[Block], dict[str, Any], int]:
    blocks: list[Block] = []
    non_empty_rows = 0
    non_empty_cells = 0
    formula_cells = 0
    visited_cells = 0
    text_chars = 0

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
        text_chars += sum(len(cell) for cell in cells)
        if text_chars > MAX_TEXT_CHARS_TOTAL:
            raise SpreadsheetLimitError(f"工作表“{sheet_name}”文本总量超过 {MAX_TEXT_CHARS_TOTAL} 字符限制")
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
        risks = region_risks(region, merged_ranges)
        if header_row is None:
            risks.append("unconfirmed_header")
        layout_extra: dict[str, Any] = {}
        if risks:
            # No guessed header or forward-filled values for irregular content.
            # Keep all original rows and absolute column labels as evidence.
            header_row = None
            columns = [_excel_column_name(i) + "列" for i in range(max(len(cells) for _, cells in region))]
            layout_extra = {"dataset_eligible": False, "layout_risks": risks}
        data_rows = [item for item in region if item[0] != header_row]
        region_metadata.append(
            {
                "region_index": region_index,
                "row_start": region[0][0],
                "row_end": region[-1][0],
                "header_row": header_row,
                "column_names": columns,
                **layout_extra,
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
            current_layout_extra: dict[str, Any] = layout_extra,
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
                        **current_layout_extra,
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
            "text_chars": text_chars,
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


def _xlsx_sheet_preflight(sheet: object) -> dict[str, Any]:
    """Stream a worksheet XML to find effective bounds and structural metadata.

    The XLSX format stores a ``<dimension>`` element that often reports the
    full styled extent of a sheet, including cells that carry formatting
    without any value, formula, or inline string. Treating that metadata as
    truth causes a single styled cell in column ``XFD`` to expand the
    worksheet to 16k columns and trip the cell limit.

    This preflight ignores style-only cells. It walks the XML once with
    :func:`xml.etree.ElementTree.iterparse`, tracking only cells that have a
    real payload (``<v>``, ``<f>``, or ``<is>``). The dimension element is
    read but not trusted: ``declared_dimension`` and the derived
    ``actual_dimension`` are both kept so the owner can later classify
    irregular structures (phantom cells, underreported extents).

    The stream is consumed linearly and every element is cleared after it is
    processed, so the in-memory tree never grows with the sheet size. The
    archive entry is opened through the parent workbook's ``ZipFile`` so the
    lifetime is tied to the workbook and no extra file handle is leaked.
    """

    path = getattr(sheet, "_worksheet_path", None)
    archive = getattr(getattr(sheet, "parent", None), "_archive", None)

    if not path or archive is None:
        raise ValueError("无法读取工作表结构")

    min_row = max_row = min_col = max_col = None
    real_cell_count = 0
    declared_dimension: str | None = None
    merge_ranges: list[str] = []

    row_index = 0
    col_index = 0
    stack: list[Any] = []
    with archive.open(path) as stream:
        for event, elem in ET.iterparse(stream, events=("start", "end")):
            tag = elem.tag
            if event == "start":
                stack.append(elem)
                if tag == f"{_SHEET_NS}row":
                    row_index = int(elem.get("r", row_index + 1))
                    col_index = 0
                continue
            if tag == f"{_SHEET_NS}c":
                ref = elem.get("r")
                if ref:
                    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", ref)
                    if not match:
                        raise ValueError("无效单元格坐标")
                    col_index = column_index_from_string(match[1])
                    cell_row = int(match[2])
                    if cell_row != row_index:
                        raise ValueError("单元格行号与工作表行号不一致")
                else:
                    col_index += 1
                    cell_row = row_index
                has_real = any(
                    child.tag == f"{_SHEET_NS}f"
                    or (child.tag == f"{_SHEET_NS}v" and child.text not in (None, ""))
                    or (child.tag == f"{_SHEET_NS}is" and any(
                        node.text not in (None, "") for node in child.iter(f"{_SHEET_NS}t")
                    ))
                    for child in elem
                )
                if has_real:
                    real_cell_count += 1
                    if real_cell_count > MAX_CELLS_TOTAL:
                        raise SpreadsheetLimitError("工作表有效单元格数量超过限制")
                    if not 1 <= cell_row <= MAX_ROWS_PER_SHEET or not 1 <= col_index <= 16384:
                        raise SpreadsheetLimitError("工作表有效行列坐标超过限制")
                    min_row = cell_row if min_row is None else min(min_row, cell_row)
                    max_row = cell_row if max_row is None else max(max_row, cell_row)
                    min_col = col_index if min_col is None else min(min_col, col_index)
                    max_col = col_index if max_col is None else max(max_col, col_index)
                elem.clear()
            elif tag == f"{_SHEET_NS}mergeCell":
                ref = elem.get("ref")
                if ref:
                    merge_ranges.append(ref)
                    if len(merge_ranges) > MAX_MERGE_RANGES:
                        raise SpreadsheetLimitError("工作表合并范围数量超过限制")
                elem.clear()
            elif tag == f"{_SHEET_NS}dimension":
                ref = elem.get("ref")
                if ref:
                    declared_dimension = ref
                elem.clear()
            elif tag in (
                f"{_SHEET_NS}row",
                f"{_SHEET_NS}sheetData",
                f"{_SHEET_NS}mergeCells",
                f"{_SHEET_NS}worksheet",
            ):
                # Container elements: clear children once we are past them so
                # the iterparse cursor does not retain the whole document.
                elem.clear()
            if tag in {f"{_SHEET_NS}c", f"{_SHEET_NS}row", f"{_SHEET_NS}mergeCell"} and len(stack) > 1:
                stack[-2].remove(elem)
            stack.pop()

    effective_bounds: tuple[int, int, int, int] | None = None
    actual_dimension: str | None = None
    if min_row is not None:
        effective_bounds = (min_row, max_row, min_col, max_col)
        actual_dimension = (
            f"{get_column_letter(min_col)}{min_row}:"
            f"{get_column_letter(max_col)}{max_row}"
        )

    return {
        "effective_bounds": effective_bounds,
        "real_cell_count": real_cell_count,
        "declared_dimension": declared_dimension,
        "merge_ranges": merge_ranges,
        "actual_dimension": actual_dimension,
    }


class XlsxParser(BaseParser):
    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        return bool(
            (file_name or "").lower().endswith(".xlsx")
            or "spreadsheetml.sheet" in (content_type or "").lower()
        )

    def parse(self, content: bytes | str, content_type: str | None = None) -> ParserResult:
        if isinstance(content, str):
            content = content.encode()
        workbook: Any = None
        values_workbook: Any = None
        rows: Any = None
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
                "text_chars": 0,
                "data_region_count": 0,
            }
            workbook_regions: list[dict[str, Any]] = []
            sheet_dimensions: list[dict[str, Any]] = []
            visited_cells = 0
            sheet_names = list(workbook.sheetnames)
            for sheet, values_sheet in zip(
                workbook.worksheets,
                values_workbook.worksheets,
            ):
                sheet_name = sheet.title
                preflight = _xlsx_sheet_preflight(sheet)
                bounds = preflight["effective_bounds"]
                if bounds is not None:
                    min_r, max_r, min_c, max_c = bounds
                    if max_r > MAX_ROWS_PER_SHEET:
                        raise SpreadsheetLimitError(
                            f"工作表“{sheet_name}”有效行 {max_r} 超过 {MAX_ROWS_PER_SHEET} 上限"
                        )
                    # Absolute coordinates are retained: no rebasing C5 to A1.
                    span = max_r * max_c
                    if span > MAX_CELLS_TOTAL:
                        raise SpreadsheetLimitError(
                            f"工作表“{sheet_name}”有效跨度 {span} 个单元格，"
                            f"超过 {MAX_CELLS_TOTAL} 上限"
                        )
                    rows = _xlsx_rows(
                        sheet,
                        values_sheet,
                        min_row=1,
                        max_row=max_r,
                        min_col=1,
                        max_col=max_c,
                    )
                else:
                    rows = iter([])

                sheet_blocks, sheet_totals, sheet_cells = _sheet_blocks(
                    sheet_name,
                    rows,
                    paragraph_offset=visited_cells,
                    merged_ranges=[
                        (r1, c1, r2, c2)
                        for c1, r1, c2, r2 in map(range_boundaries, preflight["merge_ranges"])
                    ],
                )
                visited_cells += sheet_cells
                if visited_cells > MAX_CELLS_TOTAL:
                    raise SpreadsheetLimitError(
                        f"工作簿超过 {MAX_CELLS_TOTAL} 个单元格限制"
                    )
                blocks.extend(sheet_blocks)
                for key in totals:
                    totals[key] += sheet_totals[key]
                if totals["text_chars"] > MAX_TEXT_CHARS_TOTAL:
                    raise SpreadsheetLimitError("工作簿文本总量超过限制")
                workbook_regions.extend(
                    {"sheet_name": sheet_name, **region}
                    for region in sheet_totals["regions"]
                )
                sheet_dimensions.append(
                    {
                        "sheet_name": sheet_name,
                        "declared_dimension": preflight["declared_dimension"],
                        "actual_dimension": preflight["actual_dimension"],
                        "bounds": (
                            {
                                "min_row": bounds[0],
                                "max_row": bounds[1],
                                "min_col": bounds[2],
                                "max_col": bounds[3],
                            }
                            if bounds is not None
                            else None
                        ),
                        "real_cell_count": preflight["real_cell_count"],
                        "merge_ranges": list(preflight["merge_ranges"]),
                        "dimensions_match": (
                            preflight["declared_dimension"]
                            == preflight["actual_dimension"]
                        ),
                    }
                )
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
                        "sheet_dimensions": sheet_dimensions,
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
        finally:
            if rows is not None and hasattr(rows, "close"):
                rows.close()
            for handle in (workbook, values_workbook):
                if handle is None:
                    continue
                close = getattr(handle, "close", None)
                if close is None:
                    continue
                try:
                    close()
                except Exception:  # noqa: BLE001 - close failures must not mask results
                    pass


def _xlsx_rows(
    sheet: object,
    values_sheet: object,
    *,
    min_row: int | None = None,
    max_row: int | None = None,
    min_col: int | None = None,
    max_col: int | None = None,
) -> Iterator[list[object]]:
    """Yield formulas and include cached results when the producer stored them.

    The bounds are derived from the streaming XML preflight and exclude
    style-only cells. ``min_row``/``max_row``/``min_col``/``max_col`` mirror
    the keyword arguments of :meth:`openpyxl.worksheet.worksheet.Worksheet.iter_rows`;
    passing them keeps the iteration to cells that actually carry a value,
    formula, or inline string, independent of any false ``<dimension>`` extent.
    """
    if min_row is None or max_row is None:
        return
    formula_rows = sheet.iter_rows(
        min_row=min_row,
        max_row=max_row,
        min_col=min_col,
        max_col=max_col,
    )
    value_rows = values_sheet.iter_rows(
        min_row=min_row,
        max_row=max_row,
        min_col=min_col,
        max_col=max_col,
    )
    try:
        for formula_row, value_row in zip_longest(formula_rows, value_rows, fillvalue=()):
            combined: list[object] = []
            for formula_cell, value_cell in zip_longest(formula_row, value_row, fillvalue=None):
                formula = getattr(formula_cell, "value", None)
                cached = getattr(value_cell, "value", None)
                number_format = getattr(formula_cell, "number_format", None)
                if isinstance(formula, str) and formula.startswith("="):
                    cached_display = _xlsx_display_value(cached, number_format)
                    combined.append(f"{formula} ⇒ {cached_display}" if cached is not None else formula)
                else:
                    combined.append(_xlsx_display_value(formula, number_format) if formula is not None else "")
            yield combined
    finally:
        for iterator in (formula_rows, value_rows):
            close = getattr(iterator, "close", None)
            if close is not None:
                close()


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
                formatting_info=True,
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
                "text_chars": 0,
                "data_region_count": 0,
            }
            workbook_regions: list[dict[str, Any]] = []
            visited_cells = 0
            for sheet in workbook.sheets():
                sheet_blocks, sheet_totals, sheet_cells = _sheet_blocks(
                    sheet.name,
                    _xls_rows(sheet, workbook),
                    paragraph_offset=visited_cells,
                    merged_ranges=[(r1 + 1, c1 + 1, r2, c2) for r1, r2, c1, c2 in sheet.merged_cells],
                )
                visited_cells += sheet_cells
                if visited_cells > MAX_CELLS_TOTAL:
                    raise SpreadsheetLimitError(
                        f"工作簿超过 {MAX_CELLS_TOTAL} 个单元格限制"
                    )
                blocks.extend(sheet_blocks)
                for key in totals:
                    totals[key] += sheet_totals[key]
                if totals["text_chars"] > MAX_TEXT_CHARS_TOTAL:
                    raise SpreadsheetLimitError("工作簿文本总量超过限制")
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
