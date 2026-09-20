"""Conservative layout risk detection, never inferred cell values.

This is not a table understanding model. A risk routes the entire region to
source-text retrieval until a reliable tabular interpretation is available.
"""
from __future__ import annotations

from collections.abc import Sequence


def region_risks(
    rows: list[tuple[int, list[str]]],
    merged_ranges: Sequence[tuple[int, int, int, int]] = (),
) -> list[str]:
    if not rows:
        return []
    risks: list[str] = []
    start, end = rows[0][0], rows[-1][0]
    used: set[int] = set()
    has_single_cell_row = False
    for _, cells in rows:
        occupied = {i for i, value in enumerate(cells) if value.strip()}
        used.update(occupied)
        has_single_cell_row |= len(occupied) == 1
    first_col, last_col = (min(used) + 1, max(used) + 1) if used else (0, 0)
    if used and any(
        r1 <= end and r2 >= start and c1 <= last_col and c2 >= first_col
        and (r1 < r2 or c1 < c2)
        for r1, c1, r2, c2 in merged_ranges
    ):
        risks.append("merged_cells")
    if used and any(i not in used for i in range(min(used), max(used) + 1)):
        risks.append("separated_columns")
    if len(used) > 1 and has_single_cell_row:
        risks.append("mixed_single_cell_rows")
    # Only an exact row label is a signal; a column called "合计" is not.
    summary_labels = {"合计", "总计", "小计", "subtotal", "grand total", "total"}
    for _, cells in rows[1:]:
        first = next((value.strip().casefold() for value in cells if value.strip()), "")
        if first in summary_labels:
            risks.append("summary_rows")
            break
    return risks
