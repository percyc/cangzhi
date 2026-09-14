from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UnicodeSanitizationReport:
    """Bounded counts describing one sanitization pass.

    The fields are aggregate character counts only; no payload text
    is ever recorded. ``strings_visited`` is exposed so an operator
    can see the breadth of the pass; the per-character counts give
    a stable upper bound on how much was rewritten.
    """

    nul_removed: int = 0
    surrogate_pairs_repaired: int = 0
    lone_surrogates_replaced: int = 0
    strings_visited: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "nul_removed": self.nul_removed,
            "surrogate_pairs_repaired": self.surrogate_pairs_repaired,
            "lone_surrogates_replaced": self.lone_surrogates_replaced,
            "strings_visited": self.strings_visited,
        }


def _count_surrogate_changes(value: str) -> tuple[int, int]:
    """Return ``(repaired_pairs, lone_surrogates)`` for ``value``.

    A "repaired pair" is a well-formed UTF-16 surrogate pair
    (high surrogate followed by low surrogate) that we collapse
    into the corresponding scalar code point. Anything else in
    the surrogate range is a lone surrogate.
    """

    repaired = 0
    lone = 0
    characters = value if isinstance(value, str) else ""
    length = len(characters)
    index = 0
    while index < length:
        code_point = ord(characters[index])
        if 0xD800 <= code_point <= 0xDBFF and index + 1 < length:
            next_code = ord(characters[index + 1])
            if 0xDC00 <= next_code <= 0xDFFF:
                repaired += 1
                index += 2
                continue
        if 0xD800 <= code_point <= 0xDFFF:
            lone += 1
        index += 1
    return repaired, lone


def sanitize_unicode_text(value: str) -> tuple[str, UnicodeSanitizationReport]:
    """Normalize a string for safe persistence and downstream use.

    * Strips NUL (``U+0000``) characters.
    * Collapses any well-formed UTF-16 surrogate pair (high + low)
      into the corresponding scalar code point so e.g. ``"\\ud83d\\ude00"``
      becomes ``"😀"``.
    * Replaces any lone surrogate with ``U+FFFD`` so a malformed
      PDF, HTML or docx cannot break the JSON encoder or the
      full-text search index.

    All other valid Unicode (CJK, combining marks, emoji, etc.)
    is preserved unchanged. The returned report contains only
    aggregate character counts; it never echoes the input.
    """

    if not isinstance(value, str) or not value:
        return value, UnicodeSanitizationReport()

    nul_count = value.count("\x00")
    if nul_count:
        value = value.replace("\x00", "")

    repaired, lone = _count_surrogate_changes(value)
    if repaired or lone:
        new_chars: list[str] = []
        length = len(value)
        index = 0
        while index < length:
            code_point = ord(value[index])
            if 0xD800 <= code_point <= 0xDBFF and index + 1 < length:
                next_code = ord(value[index + 1])
                if 0xDC00 <= next_code <= 0xDFFF:
                    scalar = (
                        0x10000
                        + ((code_point - 0xD800) << 10)
                        + (next_code - 0xDC00)
                    )
                    new_chars.append(chr(scalar))
                    index += 2
                    continue
            if 0xD800 <= code_point <= 0xDFFF:
                new_chars.append("\ufffd")
                index += 1
                continue
            new_chars.append(value[index])
            index += 1
        value = "".join(new_chars)

    return (
        value,
        UnicodeSanitizationReport(
            nul_removed=nul_count,
            surrogate_pairs_repaired=repaired,
            lone_surrogates_replaced=lone,
            strings_visited=1,
        ),
    )


def _merge_reports(
    left: UnicodeSanitizationReport,
    right: UnicodeSanitizationReport,
) -> UnicodeSanitizationReport:
    return UnicodeSanitizationReport(
        nul_removed=left.nul_removed + right.nul_removed,
        surrogate_pairs_repaired=(
            left.surrogate_pairs_repaired + right.surrogate_pairs_repaired
        ),
        lone_surrogates_replaced=(
            left.lone_surrogates_replaced + right.lone_surrogates_replaced
        ),
        strings_visited=left.strings_visited + right.strings_visited,
    )


def sanitize_unicode_payload(
    payload: Any,
) -> tuple[Any, UnicodeSanitizationReport]:
    """Recursively sanitize every string inside ``payload``.

    Returns a deep-copied structure with strings replaced by
    their normalized form and a report aggregating the changes
    across the whole tree. Non-string scalars (numbers, bools,
    ``None``) are returned untouched. The input is never
    mutated; this keeps the caller's view of the original
    parser output intact for logging or debugging.
    """

    if isinstance(payload, str):
        return sanitize_unicode_text(payload)
    if isinstance(payload, list):
        new_items: list[Any] = []
        report = UnicodeSanitizationReport()
        for item in payload:
            cleaned, sub = sanitize_unicode_payload(item)
            new_items.append(cleaned)
            report = _merge_reports(report, sub)
        return new_items, report
    if isinstance(payload, tuple):
        new_items_list: list[Any] = []
        report = UnicodeSanitizationReport()
        for item in payload:
            cleaned, sub = sanitize_unicode_payload(item)
            new_items_list.append(cleaned)
            report = _merge_reports(report, sub)
        return tuple(new_items_list), report
    if isinstance(payload, dict):
        new_dict: dict[Any, Any] = {}
        report = UnicodeSanitizationReport()
        for key, value in payload.items():
            if isinstance(key, str):
                cleaned_key, key_report = sanitize_unicode_text(key)
            else:
                cleaned_key, key_report = key, UnicodeSanitizationReport()
            if cleaned_key in new_dict:
                raise UnicodeKeyCollisionError("规范化后的元数据字段名冲突")
            cleaned_value, value_report = sanitize_unicode_payload(value)
            new_dict[cleaned_key] = cleaned_value
            report = _merge_reports(report, key_report)
            report = _merge_reports(report, value_report)
        return new_dict, report
    return payload, UnicodeSanitizationReport()


class UnicodeKeyCollisionError(ValueError):
    """Reject ambiguous metadata instead of silently discarding a field."""
