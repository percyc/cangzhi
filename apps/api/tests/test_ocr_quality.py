from io import BytesIO
from dataclasses import replace
from subprocess import CompletedProcess
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont
import os
import pytest

from apps.api.parsers.ocr_quality import line_risks, ruled_grid, regions, restore_region, prefer_candidate
from apps.api.parsers.pdf import PdfParser, PdfOcrOptions, _PageRender, _PageOcrResult
from apps.api.services.processing_status import _pdf_extraction_summary
from test_parsers import _make_pdf_with_image
from apps.api.parsers.ocr_quality import select_ocr_candidate


def test_risky_local_high_score_does_not_override_valid_external():
    local = [{"text": "local evidence " * 12, "confidence": .95}]
    external = [{"text": "external evidence " * 6, "confidence": .6}]
    chosen, reason = select_ocr_candidate(local, external, local_risk=True)
    assert chosen is external and reason == "risk_external_preferred"
    assert select_ocr_candidate(local, external)[0] is local


@pytest.mark.parametrize("external,reason", [
    ([], "external_empty"),
    ([{"text": "unreliable", "confidence": .1}], "external_low_quality"),
    ([{"text": "\ufffd\ufffd\ufffd broken"}], "external_invalid_characters"),
    ([{"text": "identical long repeated line"}] * 5, "external_repetition"),
    ([{"text": "short"}], "external_coverage_uncertain"),
])
def test_external_preference_has_rejection_guards(external, reason):
    local = [{"text": "original evidence " * 20, "confidence": .9}]
    chosen, actual = select_ocr_candidate(local, external, local_risk=True)
    assert chosen is local and actual == reason


def test_provenance_uses_actual_blocks_and_tolerates_malformed_metadata():
    structured = {"metadata": {"pdf_extraction": {"external_errors": []}}, "blocks": [
        {"page": 1, "extra": {"ocr_source": "local", "external_status": "timeout"}},
        {"page": 2, "extra": {"ocr_source": "external", "external_status": []}},
    ]}
    summary = _pdf_extraction_summary(SimpleNamespace(structured_content=structured))
    assert summary["local_source_pages"] == [1]
    assert summary["external_source_pages"] == [2]
    assert summary["external_errors"] == {"timeout": [1]}


def test_uncertain_rotation_does_not_bias_external_image_or_coordinates():
    original = render_grid(False)
    rotated = render_grid(True)
    received = []
    def recognize(png, *args):
        received.append(png)
        return [{"text": "synthetic evidence", "bbox": [0, 0, 1000, 1000]}]
    options = PdfOcrOptions(external_provider=SimpleNamespace())
    parser = PdfParser(options, external_ocr_runner=recognize)
    parser._selected_render = (1, rotated, 180, original)
    parser._uncertain_direction_page = 1
    outcome = parser._run_external_ocr(page_number=1, options=options, ocr_renderer=lambda _: original)
    assert received == [original.png]
    assert outcome.lines[0]["bbox"] == [0, 0, 400, 300]
    assert "rotation_correction" not in outcome.lines[0]


def render_grid(grid=True):
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    if grid:
        for x in range(80, 721, 160):
            draw.line((x, 60, x, 540), fill="black", width=3)
        for y in range(60, 541, 120):
            draw.line((80, y, 720, y), fill="black", width=3)
    output = BytesIO()
    image.save(output, "PNG")
    return _PageRender(800, 600, 400, 300, output.getvalue())


def test_local_bad_lines_not_hidden_by_high_mean():
    lines = [{"text":"clear", "confidence":.98}] * 18 + [{"text":"bad", "confidence":.2}] * 2
    assert sum(line["confidence"] for line in lines)/len(lines) > .8
    assert "local_low_quality" in line_risks(lines)
    assert not line_risks(lines[:18])
    assert "small_text" in line_risks([{"word_height_px":12}] * 4)
    assert not line_risks([{"word_height_px":24}] * 4)


def test_word_tail_not_hidden_by_line_mean():
    assert "local_low_quality" in line_risks([{"confidence":.8,"low_word_ratio":.3}] * 4)


def test_grid_detection_and_corrupt_images_are_conservative():
    assert ruled_grid(render_grid().png)
    assert not ruled_grid(render_grid(False).png)
    assert not ruled_grid(b"not image")


def test_region_mapping_and_overlap_ownership():
    render = render_grid()
    outputs = list(regions(render))
    assert len(outputs) == 4
    crop, (left, top), core = outputs[3]
    bottom = render.height_px-top-crop.height_px
    mapped = restore_region([{"text":"original", "bbox":[30,20,60,40]}], render, (left,bottom), core)
    assert len(mapped) == 1
    assert mapped[0]["bbox"] == [left/2+30, bottom/2+20,left/2+60,bottom/2+40]
    # Center outside this tile's core belongs to another tile, even if overlap saw it.
    assert not restore_region([{"bbox":[0,140,1,141]}], render, (left,bottom), core)


def test_more_garbage_or_short_confident_response_cannot_win():
    old = [{"text":"reliable original evidence", "confidence":.9}]
    assert not prefer_candidate(old, [{"text":"garbage" * 5, "confidence":.1}])
    assert not prefer_candidate(old, [{"text":"short", "confidence":1.0}])
    assert prefer_candidate(old, [{"text":"reliable original evidence and more", "confidence":.95}])
    assert not prefer_candidate(old, [{"text":"noise " * 100, "confidence":.4}])


def test_clean_page_no_extra_work(monkeypatch):
    render = render_grid(False)
    parser = PdfParser(PdfOcrOptions())
    parser._selected_render = (1, render, 0, render)
    monkeypatch.setattr(parser, "_ocr_page", lambda *a,**k: pytest.fail("unexpected extra OCR"))
    original = _PageOcrResult(1,[{"text":"Reliable clear text", "confidence":.95}])
    result, risks, refined, regional = parser._refine_local(b"",1,PdfOcrOptions(),lambda _:render,original)
    assert result is original and not risks and not refined and not regional


def test_refinement_keeps_original_risks_after_quality_improves(monkeypatch):
    render = render_grid(False)
    high = replace(render, width_px=1600)
    parser = PdfParser(PdfOcrOptions())
    parser._selected_render = (1, render, 0, render)
    original = _PageOcrResult(1, [{"text": "uncertain", "confidence": .2}] * 2)
    improved = _PageOcrResult(1, [{"text": "clear longer synthetic evidence", "confidence": .95}])
    monkeypatch.setattr(parser, "_build_ocr_renderer", lambda *a: lambda _: high)
    monkeypatch.setattr(parser, "_ocr_page", lambda *a, **k: improved)
    result, risks, refined, _ = parser._refine_local(b"", 1, PdfOcrOptions(), lambda _: render, original)
    assert result is improved and refined
    assert "local_low_quality" in risks and not line_risks(result.lines)


def test_refinement_timeout_never_publishes_partial_regions(monkeypatch):
    render = render_grid()
    parser = PdfParser(PdfOcrOptions(), renderer_factory=lambda *_: lambda _: render)
    original = _PageOcrResult(1,[{"text":"existing evidence", "confidence":.9}])
    parser._selected_render = (1, render, 0, render)
    calls = []
    def fail(page, options, renderer, **kwargs):
        calls.append(options.timeout_seconds)
        return _PageOcrResult(page, [], "ocr_timeout")
    monkeypatch.setattr(parser,"_ocr_page",fail)
    result, risks, _, regional = parser._refine_local(b"",1,PdfOcrOptions(),lambda _:render,original)
    assert result is original and "table_layout" in risks and not regional
    assert len(calls) == 1 and 0 < calls[0] <= 15


@pytest.mark.parametrize("budget",[0,1])
def test_grid_risk_triggers_external_even_with_high_average(budget):
    render = render_grid()
    calls = []
    def runner(*args):
        return CompletedProcess([],0,b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t30\t30\t100\t25\t95\tClear local evidence\n",b"")
    provider = SimpleNamespace(describe=lambda:{"provider":"synthetic","model":"test"})
    def external(*args):
        calls.append(1)
        return [{"text":"Clear external evidence", "confidence":950}]
    parser = PdfParser(PdfOcrOptions(external_provider=provider,external_max_pages=budget),
        renderer_factory=lambda *_: lambda _:render,tesseract_lookup=lambda:"tesseract",
        tesseract_runner=runner,external_ocr_runner=external)
    result=parser.parse(_make_pdf_with_image())
    extraction=result.structured_content.metadata["pdf_extraction"]
    assert len(calls)==budget
    assert extraction["review_pages"]==[1] and extraction["ocr_status"]=="partial"
    if budget:
        assert extraction["external_trigger_reasons"]["local_quality_risk"]==[1]
        assert extraction["external_selected_pages"] == [1]
        assert extraction["selection_decisions"][0]["reason"] in {"risk_external_preferred", "local_unusable"}
        assert extraction["selection_decisions"][0]["source"] == "external"
    summary=_pdf_extraction_summary(SimpleNamespace(structured_content=result.structured_content.to_dict()))
    assert summary["review_reasons"]["table_layout"]==[1]


@pytest.mark.skipif(os.environ.get("CANGZHI_TEST_OCR_ORIENTATION") != "1", reason="requires Tesseract")
def test_real_dense_grid_preserves_numeric_evidence_without_claiming_dataset():
    image = Image.new("RGB", (2000, 1200), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("LiberationSans-Regular.ttf", 28)
    values = []
    for row in range(6):
        for col in range(4):
            value = str(51001+row*4+col)
            values.append(value)
            draw.text((100+col*450, 130+row*160), f"Value {value}", font=font, fill="black")
    for x in range(70, 1871, 450):
        draw.line((x, 70, x, 1030), fill="black", width=3)
    for y in range(70, 1031, 160):
        draw.line((70, y, 1870, y), fill="black", width=3)
    output=BytesIO()
    image.save(output,"PDF",resolution=300)
    result=PdfParser(PdfOcrOptions(language="eng")).parse(output.getvalue())
    assert result.success
    text=result.structured_content.full_text()
    assert sum(value in text for value in values) >= 22, text
    assert result.structured_content.document_type=="pdf"
    assert result.structured_content.metadata["pdf_extraction"]["review_pages"]==[1]
