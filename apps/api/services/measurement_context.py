"""Verify unit/currency context of matched dataset rows (ADR-038).

This module is deliberately pure: it receives already-computed per-column
statistics and returns a verdict. It never reads the database, never calls a
model and never loads rows. Both execution backends (DuckDB pushdown and the
PostgreSQL/row fallback) hand it the same shape so a result is comparable
across backends.

Three rules make the verdict conservative:

* Only *exact* generic column names count as a unit/currency carrier
  (单位/计量单位/unit/units/uom/币种/currency/currency_code). Names are
  normalised, never guessed from the question, a place name, a numeric cell,
  an AI field suggestion or a sample value.
* A single non-empty value with no missing rows is ``known``. Mixed values or
  partial blanks are ``blocked`` unless the column itself is the group key, in
  which case each group's value *is* that group's unit and stays ``grouped``.
  No unit conversion is ever attempted.
* Counting metrics (``count``/``count_distinct``/``rows``) never inherit the
  counted object's physical or monetary unit, so they are ``not_applicable``.

When more candidate columns than :data:`MAX_CONTEXT_COLUMNS` exist the excess
is not silently dropped: the verdict becomes ``blocked`` with
``candidates_truncated``, because a narrow default would look like a confirmed
unit while the real unit column was never checked.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Upper bound on how many unit/currency columns one verdict describes.
MAX_CONTEXT_COLUMNS = 4
#: Upper bound on how many example values are reported per column.
MAX_SAMPLE_VALUES = 5
#: Upper bound on the rendered length of one example value.
MAX_SAMPLE_LENGTH = 40

#: Metrics that summarise a counted object rather than its magnitude. These
#: must not inherit currency/physical units (ADR-038).
COUNTING_METRICS = frozenset({"rows", "count", "count_distinct"})
#: Metrics whose value is a magnitude and therefore needs a verified unit.
MEASURE_METRICS = frozenset({"sum", "avg", "min", "max"})

#: Verdict states. ``blocked`` means "do not present this number as a single
#: trustworthy total"; it is not an error.
STATE_KNOWN = "known"
STATE_UNKNOWN = "unknown"
STATE_GROUPED = "grouped"
STATE_BLOCKED = "blocked"
STATE_NOT_APPLICABLE = "not_applicable"

# Exact generic names only. The set is intentionally small: an unrecognised
# column must stay unknown rather than being resolved by guessing.
_EXACT_NAMES: frozenset[str] = frozenset(
    {
        "单位",
        "计量单位",
        "币种",
        "币种名称",
        "货币",
        "货币单位",
        "计量单位名称",
        "uom",
        "unit",
        "units",
        "unitofmeasure",
        "uomname",
        "measureunit",
        "currency",
        "currencycode",
        "currencyunit",
        "currencyname",
    }
)

_STRIPPED_RE = re.compile(r"[\s_\-—–·/\\.]+")


def normalise_column_name(name: Any) -> str:
    """Return a comparison key for a column name.

    Normalisation is mechanical (case folding plus separator removal). It never
    inspects values, so two columns that normalise identically really are the
    same generic name.
    """

    text = str(name or "").strip().casefold()
    return _STRIPPED_RE.sub("", text)


def is_context_column(name: Any) -> bool:
    """Return whether a column name is a generic unit/currency carrier.

    Accepts only the exact generic name. Measure-specific suffixes require an
    explicit association and are not guessed here.
    A name that merely *contains* a generic word (for example ``单位成本``
    describing a cost column, or ``地区``) is rejected.
    """

    text = str(name or "").strip()
    if not text:
        return False
    return normalise_column_name(text) in _EXACT_NAMES


@dataclass(frozen=True)
class ColumnStats:
    """Per-column statistics computed over exactly the matched rows."""

    column: str
    matched_row_count: int = 0
    distinct_count: int = 0
    blank_count: int = 0
    samples: tuple[str, ...] = ()

    @property
    def non_empty_count(self) -> int:
        return max(0, self.matched_row_count - self.blank_count)


@dataclass(frozen=True)
class MeasureContext:
    """Verdict for one executed plan, safe to serialise into evidence."""

    state: str = STATE_NOT_APPLICABLE
    reason: str = ""
    candidates_truncated: bool = False
    columns: tuple[dict[str, Any], ...] = ()
    metric: str = ""

    @property
    def blocked(self) -> bool:
        return self.state == STATE_BLOCKED

    @property
    def known(self) -> bool:
        return self.state == STATE_KNOWN

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state,
            "metric": self.metric,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.candidates_truncated:
            payload["candidates_truncated"] = True
        if self.columns:
            payload["columns"] = [dict(item) for item in self.columns]
        return payload

    def diagnosis(self) -> str:
        """Return a short Chinese diagnosis used in refinement feedback."""

        if self.state == STATE_BLOCKED:
            detail = "；".join(
                f"{item['column']}：{item.get('state')}"
                + (
                    f"（示例 {', '.join(item['samples'])}）"
                    if item.get("samples")
                    else ""
                )
                for item in self.columns
            )
            return (
                "命中范围内单位/币种不唯一或存在缺失，"
                f"不能直接给出合计（{detail or '候选单位列过多'}）。"
            )
        return ""


def select_context_candidates(
    columns: Sequence[str],
    *,
    limit: int = MAX_CONTEXT_COLUMNS,
) -> tuple[list[str], bool]:
    """Return ``(candidate_names, truncated)`` for generic unit columns."""

    candidates = [name for name in columns if is_context_column(name)]
    if len(candidates) <= limit:
        return candidates, False
    return candidates[:limit], True


def _clean_samples(values: Sequence[Any]) -> tuple[str, ...]:
    samples: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in samples:
            continue
        samples.append(text[:MAX_SAMPLE_LENGTH])
        if len(samples) >= MAX_SAMPLE_VALUES:
            break
    return tuple(samples)


def _column_payload(
    stats: ColumnStats, state: str, *, grouped: bool = False
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "column": stats.column,
        "state": state,
        "distinct_count": stats.distinct_count,
        "blank_count": stats.blank_count,
        "matched_row_count": stats.matched_row_count,
    }
    samples = _clean_samples(stats.samples)
    if samples:
        payload["samples"] = list(samples)
    if state == STATE_KNOWN and samples:
        if len(str(stats.samples[0]).strip()) <= MAX_SAMPLE_LENGTH:
            payload["unit"] = samples[0]
        else:
            payload["state"] = STATE_UNKNOWN
            payload["samples_truncated"] = True
    if grouped:
        payload["grouped"] = True
    return payload


def evaluate_measure_context(
    *,
    metric: str,
    matched_row_count: int,
    group_by: Sequence[str] = (),
    stats_by_column: Mapping[str, ColumnStats] | None = None,
    column_order: Sequence[str] = (),
    candidates_truncated: bool = False,
) -> MeasureContext:
    """Classify the unit/currency context of exactly the matched rows.

    ``stats_by_column`` must contain complete distinct counts over the matched
    rows — a ``LIMIT``-ed sample is explicitly *not* acceptable, because a
    truncated sample would happily report a single "unit" for a mixed column.
    """

    stats_by_column = stats_by_column or {}
    metric = str(metric or "")

    if metric in COUNTING_METRICS or metric not in MEASURE_METRICS:
        return MeasureContext(
            state=STATE_NOT_APPLICABLE,
            metric=metric,
            reason="计数类统计不继承被计数对象的单位或币种",
        )

    if matched_row_count <= 0:
        return MeasureContext(
            state=STATE_UNKNOWN,
            metric=metric,
            reason="筛选条件没有命中任何行，无法确认单位",
        )

    ordered = [name for name in list(column_order) if name in stats_by_column] or list(
        stats_by_column
    )
    payloads: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []

    for column in ordered:
        stats = stats_by_column[column]
        is_group_key = column in set(group_by)
        if stats.non_empty_count == 0:
            state = STATE_UNKNOWN
            if is_group_key:
                # A group key whose value is empty describes that group only.
                payloads.append(_column_payload(stats, state, grouped=True))
                continue
            payloads.append(_column_payload(stats, state))
            continue
        if stats.distinct_count > 1:
            state = STATE_GROUPED if is_group_key else STATE_BLOCKED
            if not is_group_key:
                blocked_reasons.append(column)
            payloads.append(_column_payload(stats, state, grouped=is_group_key))
            continue
        if stats.blank_count > 0:
            # Partially filled: the single present value may not describe the
            # rows that are missing it.
            state = STATE_GROUPED if is_group_key else STATE_BLOCKED
            if not is_group_key:
                blocked_reasons.append(column)
            payloads.append(_column_payload(stats, state, grouped=is_group_key))
            continue
        state = STATE_GROUPED if is_group_key else STATE_KNOWN
        payloads.append(_column_payload(stats, state, grouped=is_group_key))

    if candidates_truncated:
        blocked_reasons.append("(候选单位列超出上限)")

    if blocked_reasons:
        return MeasureContext(
            state=STATE_BLOCKED,
            metric=metric,
            reason="单位或币种在命中范围内不唯一或部分缺失",
            candidates_truncated=candidates_truncated,
            columns=tuple(payloads),
        )
    if not payloads:
        return MeasureContext(
            state=STATE_UNKNOWN,
            metric=metric,
            reason="数据集没有明确的单位或币种列",
        )
    return MeasureContext(
        state=(
            STATE_UNKNOWN
            if any(p["state"] == STATE_UNKNOWN for p in payloads)
            else STATE_GROUPED
            if any(p["state"] == STATE_GROUPED for p in payloads)
            else STATE_KNOWN
        ),
        metric=metric,
        columns=tuple(payloads),
    )
