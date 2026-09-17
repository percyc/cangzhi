import pytest
from apps.api.parsers.pdf import native_heading_level, _build_native_blocks


@pytest.mark.parametrize("text", ["ＧＢ １４７６２ ２００８", "Ⅲ", "Ⅳ", "Ｚ ６４", "ＩＣＳ １３.０４０.５０", "CO", "NOX", "A = B", "123.45", "1.23 mg/L", "m/s", "GB 1234-2008", "温度T", "CHAPTER", "Section", "123", "附录", "A", "I", "TEST"])
def test_identifier_is_not_heading(text):
    assert native_heading_level(text) is None
    blocks = _build_native_blocks(text, 3, [])
    assert blocks[0].type == "paragraph"
    assert blocks[0].text == text
    assert blocks[0].page == 3


@pytest.mark.parametrize("text", ["第一章 总则", "第二节 适用范围", "1 总则", "1.2 测试方法", "Chapter 1 Introduction", "Section 2 Test methods", "TEST METHODS", "GENERAL REQUIREMENTS"])
def test_supported_headings(text):
    assert native_heading_level(text) is not None


def test_all_native_lines_keep_order_and_locators():
    lines = ["1 总则", "ＧＢ １４７６２ ２００８", "Ⅲ", "实际要求", "TEST METHODS", "测试内容"]
    blocks = _build_native_blocks("\n".join(lines), 4, [])
    assert [b.text for b in blocks] == lines
    assert [b.paragraph_index for b in blocks] == list(range(len(lines)))
    assert all(b.page == 4 for b in blocks)
