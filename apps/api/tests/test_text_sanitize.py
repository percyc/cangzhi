"""Regression tests for the recursive Unicode sanitization helper.

The worker calls :func:`apps.api.parsers.text_sanitize.sanitize_unicode_payload`
on the structured parser result before persisting the document
version. The sanitization must:

* remove NUL (``\\x00``) bytes;
* repair well-formed UTF-16 surrogate pairs to their scalar code
  point (e.g. ``"\\ud83d\\ude00"`` → ``"😀"``);
* replace any lone surrogate with ``U+FFFD`` so a malformed
  PDF, HTML or docx cannot break the JSON encoder or the
  full-text search index;
* leave all other valid Unicode (CJK, combining marks, emoji,
  math symbols, …) untouched;
* report only aggregate character counts; never echo the
  rewritten text in logs or metadata;
* recursively walk nested lists and dicts without mutating the
  input.
"""

from __future__ import annotations

import json

import pytest

from apps.api.parsers.text_sanitize import (
    UnicodeSanitizationReport,
    sanitize_unicode_payload,
    sanitize_unicode_text,
)


class TestSanitizeUnicodeText:
    def test_passthrough_for_clean_ascii(self) -> None:
        value, report = sanitize_unicode_text("hello world")
        assert value == "hello world"
        # The string was visited, but nothing was rewritten.
        assert report.nul_removed == 0
        assert report.surrogate_pairs_repaired == 0
        assert report.lone_surrogates_replaced == 0
        assert report.strings_visited == 1

    def test_passthrough_for_clean_cjk_and_emoji(self) -> None:
        original = "中文测试 😀 ★ — “smart quotes”"
        value, report = sanitize_unicode_text(original)
        assert value == original
        assert report.nul_removed == 0
        assert report.surrogate_pairs_repaired == 0
        assert report.lone_surrogates_replaced == 0
        assert report.strings_visited == 1

    def test_nul_is_removed(self) -> None:
        value, report = sanitize_unicode_text("a\x00b\x00c")
        assert value == "abc"
        assert report.nul_removed == 2
        assert report.surrogate_pairs_repaired == 0
        assert report.lone_surrogates_replaced == 0
        assert report.strings_visited == 1

    def test_valid_surrogate_pair_collapses_to_scalar(self) -> None:
        # U+1F600 (😀) is encoded in UTF-16 as the surrogate pair
        # 0xD83D + 0xDE00. The sanitiser must repair it to the
        # single scalar character so JSON encoding and search
        # do not see a lone surrogate.
        value, report = sanitize_unicode_text("\ud83d\ude00")
        assert value == "\U0001f600"
        assert ord(value) == 0x1F600
        assert report.surrogate_pairs_repaired == 1
        assert report.lone_surrogates_replaced == 0
        assert report.nul_removed == 0

    def test_lone_high_surrogate_replaced_with_replacement_char(self) -> None:
        value, report = sanitize_unicode_text("before\ud83dafter")
        assert value == "before\ufffdafter"
        assert report.lone_surrogates_replaced == 1
        assert report.surrogate_pairs_repaired == 0

    def test_lone_low_surrogate_replaced_with_replacement_char(self) -> None:
        value, report = sanitize_unicode_text("before\ude00after")
        assert value == "before\ufffdafter"
        assert report.lone_surrogates_replaced == 1

    def test_mixed_pair_and_lone_surrogate(self) -> None:
        value, report = sanitize_unicode_text("\ud83d\ude00\ud83d")
        assert value == "\U0001f600\ufffd"
        assert report.surrogate_pairs_repaired == 1
        assert report.lone_surrogates_replaced == 1

    def test_two_high_surrogates_become_two_replacement_chars(self) -> None:
        value, report = sanitize_unicode_text("\ud83d\ud83d")
        assert value == "\ufffd\ufffd"
        assert report.lone_surrogates_replaced == 2

    def test_empty_string_yields_empty_report(self) -> None:
        value, report = sanitize_unicode_text("")
        assert value == ""
        assert report == UnicodeSanitizationReport()

    def test_non_string_input_is_returned_unchanged(self) -> None:
        # The function is typed for ``str``; defensively it should
        # not blow up on unexpected types and should report
        # ``strings_visited == 0`` so the aggregate stays honest.
        for value in (None, 0, 1.5, b"bytes", ["list"]):
            cleaned, report = sanitize_unicode_text(value)  # type: ignore[arg-type]
            assert cleaned is value
            assert report == UnicodeSanitizationReport()

    def test_valid_unicode_around_surrogates_is_preserved(self) -> None:
        # CJK + valid pair + combining mark; we want all of it
        # kept, only the surrogate pair is collapsed.
        original = "中文\ud83d\ude00́"
        value, report = sanitize_unicode_text(original)
        assert value == "中文\U0001f600́"
        assert report.surrogate_pairs_repaired == 1


class TestSanitizeUnicodePayload:
    def test_dict_values_are_normalised_recursively(self) -> None:
        payload = {
            "title": "Title\ud83d\ude00",
            "author": "A\x00B",
            "deep": {
                "list": [
                    {"text": "hello\ud83d"},
                    "tail",
                ],
            },
        }
        cleaned, report = sanitize_unicode_payload(payload)

        assert cleaned["title"] == "Title\U0001f600"
        assert cleaned["author"] == "AB"
        assert cleaned["deep"]["list"][0]["text"] == "hello\ufffd"
        assert cleaned["deep"]["list"][1] == "tail"
        # NUL: "A\x00B" → 1
        # Pair: "Title\ud83d\ude00" → 1
        # Lone: "hello\ud83d" → 1
        assert report.nul_removed == 1
        assert report.surrogate_pairs_repaired == 1
        assert report.lone_surrogates_replaced == 1
        # title, author, deep (key), list (key), text (key),
        # tail — six string values plus three string keys.
        assert report.strings_visited == 9

    def test_lists_are_normalised_recursively(self) -> None:
        payload = ["a\ud83d\ude00b", {"k": "c\ud83d"}]
        cleaned, report = sanitize_unicode_payload(payload)
        assert cleaned == ["a\U0001f600b", {"k": "c\ufffd"}]
        assert report.surrogate_pairs_repaired == 1
        assert report.lone_surrogates_replaced == 1

    def test_tuples_are_preserved_as_tuples(self) -> None:
        cleaned, report = sanitize_unicode_payload(("a\x00b", "c"))
        assert isinstance(cleaned, tuple)
        assert cleaned == ("ab", "c")
        assert report.nul_removed == 1
        assert report.strings_visited == 2

    def test_input_is_not_mutated(self) -> None:
        original = {"k": "a\ud83db", "list": ["c\ud83d\ude00"]}
        snapshot = {
            "k": "a\ud83db",
            "list": ["c\ud83d\ude00"],
        }
        sanitize_unicode_payload(original)
        assert original == snapshot

    def test_numbers_and_bools_pass_through(self) -> None:
        cleaned, report = sanitize_unicode_payload(
            {"count": 5, "ratio": 0.5, "ok": True, "nothing": None}
        )
        assert cleaned == {
            "count": 5,
            "ratio": 0.5,
            "ok": True,
            "nothing": None,
        }
        # The four string keys were visited but nothing was
        # rewritten; the values are not strings.
        assert report.nul_removed == 0
        assert report.surrogate_pairs_repaired == 0
        assert report.lone_surrogates_replaced == 0
        assert report.strings_visited == 4

    def test_clean_payload_reports_no_changes(self) -> None:
        payload = {"a": "hello", "b": ["world", "中文"]}
        cleaned, report = sanitize_unicode_payload(payload)
        assert cleaned == payload
        assert report.nul_removed == 0
        assert report.surrogate_pairs_repaired == 0
        assert report.lone_surrogates_replaced == 0

    def test_cleaned_output_is_json_safe(self) -> None:
        # The point of the sanitiser is to keep the JSON
        # encoder happy; previously a lone surrogate would
        # raise ``UnicodeEncodeError: 'utf-8' codec can't
        # encode character ...``.
        payload = {
            "title": "Title\ud83d",
            "blocks": [
                {"text": "valid pair \ud83d\ude00 inside"},
            ],
        }
        cleaned, _ = sanitize_unicode_payload(payload)
        # ``ensure_ascii=False`` keeps the emoji readable.
        rendered = json.dumps(cleaned, ensure_ascii=False)
        assert "\ud83d" not in rendered
        assert "\ude00" not in rendered
        # The repair must round-trip through JSON.
        round_trip = json.loads(rendered)
        assert round_trip["blocks"][0]["text"] == "valid pair \U0001f600 inside"

    @pytest.mark.parametrize(
        "value,expected_pair,expected_lone",
        [
            ("\ud83d\ude00", 1, 0),
            ("\ud83d", 0, 1),
            ("\ude00", 0, 1),
            ("\ud83d\ude00\ud83d\ude00", 2, 0),
            ("\ud83d\ude00\ud83d", 1, 1),
        ],
    )
    def test_count_aggregation(
        self, value: str, expected_pair: int, expected_lone: int
    ) -> None:
        _, report = sanitize_unicode_text(value)
        assert report.surrogate_pairs_repaired == expected_pair
        assert report.lone_surrogates_replaced == expected_lone

    def test_report_as_dict_only_exposes_aggregate_counts(self) -> None:
        # Ensure the report-as-dict shape is bounded and does
        # not leak any of the input text.
        _, report = sanitize_unicode_text("a\x00b\ud83d\ude00\ud83dc")
        rendered = report.as_dict()
        assert set(rendered) == {
            "nul_removed",
            "surrogate_pairs_repaired",
            "lone_surrogates_replaced",
            "strings_visited",
        }
        assert rendered["nul_removed"] == 1
        assert rendered["surrogate_pairs_repaired"] == 1
        assert rendered["lone_surrogates_replaced"] == 1
        assert rendered["strings_visited"] == 1


def test_normalized_metadata_key_collision_is_explicit():
    import pytest
    from apps.api.parsers.text_sanitize import UnicodeKeyCollisionError

    source = {"field": "first", "field\x00": "second"}
    with pytest.raises(UnicodeKeyCollisionError):
        sanitize_unicode_payload(source)
    assert len(source) == 2
