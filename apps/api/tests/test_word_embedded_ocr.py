from io import BytesIO
from types import SimpleNamespace

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from PIL import Image
import pytest

from apps.api.parsers import word_images
from apps.api.parsers.base import Block, ParserResult, StructuredContent, get_parser_for_content
from apps.api.parsers.docx import DocxParser
from apps.api.parsers.pdf import PdfOcrOptions


def picture(color="white"):
    stream = BytesIO()
    Image.new("RGB", (100, 60), color).save(stream, "PNG")
    stream.seek(0)
    return stream


def document(*, duplicate=False, table=False, native=True):
    doc = Document()
    if native:
        doc.add_heading("Original heading", 1)
        doc.add_paragraph("Before image")
    doc.add_picture(picture())
    if duplicate:
        doc.add_picture(picture())
    if table:
        cell = doc.add_table(rows=1, cols=1).cell(0, 0)
        cell.text = "Native cell"
        cell.paragraphs[0].add_run().add_picture(picture("red"))
    if native:
        doc.add_paragraph("After image")
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def fake_ocr(monkeypatch, *, fail=False, external=False):
    calls = []
    class FakeParser:
        def __init__(self, options):
            self.options = options
        def parse(self, blob):
            calls.append(self.options)
            if external and self.options.external_provider:
                try:
                    self.options.external_provider.recognize(blob, page_number=1)
                except Exception:
                    pass
            if fail:
                raise ValueError("private upstream response must not be persisted")
            return ParserResult(True, StructuredContent("image", [Block("paragraph", "IMAGE EVIDENCE", [], page=1,
                extra={"source":"ocr", "ocr_source":"local", "bbox":[0,0,100,60]})], metadata={
                "pdf_extraction":{"ocr_status":"completed"}, "image_extraction":{"width":100,"height":60}}))
    monkeypatch.setattr(word_images, "ImageParser", FakeParser)
    return calls


def test_native_order_coordinates_duplicate_cache_and_tables(monkeypatch):
    calls = fake_ocr(monkeypatch)
    source = document(duplicate=True, table=True)
    native = DocxParser().parse(source).structured_content
    result = DocxParser(PdfOcrOptions()).parse(source)
    assert result.success
    structured = result.structured_content
    retained = [b.to_dict() for b in structured.blocks if "word_image_index" not in b.extra]
    assert retained == [b.to_dict() for b in native.blocks]
    assert len(calls) == 2
    assert [b.text for b in structured.blocks] == ["Original heading", "Before image", "IMAGE EVIDENCE",
        "IMAGE EVIDENCE", "Native cell", "IMAGE EVIDENCE", "After image"]
    images = [b for b in structured.blocks if "word_image_index" in b.extra]
    assert all(b.page is None and b.heading_path == ["Original heading"] for b in images)
    assert all(b.extra["coordinate_source"] == "embedded_visible_image" for b in images)
    assert structured.metadata["word_embedded_ocr"]["items"][1]["reused"]
    assert "private upstream" not in str(structured.to_dict())


def test_image_failure_and_disabled_preserve_native(monkeypatch):
    calls = fake_ocr(monkeypatch, fail=True)
    for enabled, status in [(True,"failed"), (False,"disabled")]:
        result = DocxParser(PdfOcrOptions(enabled=enabled)).parse(document())
        assert result.success and "Before image" in result.structured_content.full_text()
        summary = result.structured_content.metadata["word_embedded_ocr"]
        assert summary["status"] == "partial" and summary["items"][0]["status"] == status
        assert "private upstream" not in str(summary)
    assert len(calls) == 1


def test_image_only_word_retains_existing_pdf_fallback(monkeypatch):
    calls = fake_ocr(monkeypatch)
    result = DocxParser(PdfOcrOptions()).parse(document(native=False))
    assert not calls and not result.structured_content.full_text()
    assert result.structured_content.metadata["docx_extraction"]["embedded_image_count"] == 1


@pytest.mark.parametrize("limit", [0, 1])
def test_document_wide_external_budget_counts_failures(monkeypatch, limit):
    calls = fake_ocr(monkeypatch, external=True)
    external_calls = []
    def failing(*args, **kwargs):
        external_calls.append(1)
        raise ValueError("failure")
    provider = SimpleNamespace(recognize=failing, describe=lambda: {})
    result = DocxParser(PdfOcrOptions(external_provider=provider, external_max_pages=limit)).parse(document(table=True))
    assert len(calls) == 2 and len(external_calls) == limit
    assert result.structured_content.metadata["word_embedded_ocr"]["external_calls"] == limit


def test_limits_and_linked_images_are_visible_without_network(monkeypatch):
    calls = fake_ocr(monkeypatch)
    monkeypatch.setattr(word_images, "MAX_UNIQUE_IMAGES", 1)
    result = DocxParser(PdfOcrOptions()).parse(document(table=True))
    assert len(calls) == 1
    assert result.structured_content.metadata["word_embedded_ocr"]["items"][1]["status"] == "budget_exhausted"
    doc = Document(BytesIO(document()))
    blip = next(doc.element.body.iter(qn("a:blip")))
    blip.attrib.pop(qn("r:embed"))
    blip.set(qn("r:link"), "untrusted-external")
    output = BytesIO(); doc.save(output)
    result = DocxParser(PdfOcrOptions()).parse(output.getvalue())
    assert len(calls) == 1
    assert result.structured_content.metadata["word_embedded_ocr"]["items"][0]["status"] == "linked_or_missing"


@pytest.mark.parametrize("suffix", ["doc", "docx", "wps"])
def test_worker_dispatch_passes_shared_ocr_options(suffix):
    options = PdfOcrOptions(enabled=False)
    assert get_parser_for_content(None, "sample." + suffix, pdf_ocr_options=options).ocr_options is options


def test_occurrence_and_byte_limits_preserve_body(monkeypatch):
    calls = fake_ocr(monkeypatch)
    monkeypatch.setattr(word_images, "MAX_OCCURRENCES", 1)
    result = DocxParser(PdfOcrOptions()).parse(document(duplicate=True))
    assert len(calls) == 1
    assert result.structured_content.metadata["word_embedded_ocr"]["occurrence_limit_reached"]
    monkeypatch.setattr(word_images, "MAX_IMAGE_BYTES", 1)
    result = DocxParser(PdfOcrOptions()).parse(document())
    assert result.structured_content.metadata["word_embedded_ocr"]["items"][0]["status"] == "image_limit"
    assert "After image" in result.structured_content.full_text()


def test_invalid_crop_is_not_sent_to_ocr(monkeypatch):
    calls = fake_ocr(monkeypatch)
    doc = Document(BytesIO(document()))
    fill = next(doc.element.body.iter(qn("pic:blipFill")))
    rect = OxmlElement("a:srcRect"); rect.set("l", "100000"); fill.append(rect)
    output = BytesIO(); doc.save(output)
    result = DocxParser(PdfOcrOptions()).parse(output.getvalue())
    assert not calls
    assert result.structured_content.metadata["word_embedded_ocr"]["items"][0]["status"] == "cropped_image"


def test_crop_excludes_hidden_pixels_and_negative_padding_is_safe():
    image = Image.new("RGB", (100, 60), "white")
    image.paste("red", (0, 0, 10, 60))
    stream = BytesIO(); image.save(stream, "PNG")
    visible = Image.open(BytesIO(word_images._visible_blob(stream.getvalue(), (.1, 0, 0, 0))))
    assert visible.size == (90, 60) and visible.getpixel((0, 0)) == (255, 255, 255)
    doc = Document(BytesIO(document()))
    fill = next(doc.element.body.iter(qn("pic:blipFill")))
    rect = OxmlElement("a:srcRect"); rect.set("l", "-40"); fill.append(rect)
    assert word_images._crop_spec(next(doc.element.body.iter(qn("a:blip")))) == (0, 0, 0, 0)


@pytest.mark.parametrize("hidden", [False, True])
def test_deleted_or_hidden_pictures_are_not_read(monkeypatch, hidden):
    calls = fake_ocr(monkeypatch)
    doc = Document(BytesIO(document()))
    blip = next(doc.element.body.iter(qn("a:blip")))
    run = next(parent for parent in blip.iterancestors() if parent.tag == qn("w:r"))
    if hidden:
        properties = OxmlElement("w:rPr"); properties.append(OxmlElement("w:vanish")); run.insert(0, properties)
    else:
        parent = run.getparent(); parent.remove(run)
        deleted = OxmlElement("w:del"); deleted.append(run); parent.append(deleted)
    output = BytesIO(); doc.save(output)
    result = DocxParser(PdfOcrOptions()).parse(output.getvalue())
    assert result.success and not calls
    assert result.structured_content.metadata["word_embedded_ocr"]["items"][0]["status"] == "unsupported_structure"


def test_unexpected_image_scan_failure_does_not_discard_body(monkeypatch):
    def fail(*args):
        raise ValueError("private data")
    monkeypatch.setattr(word_images, "add_embedded_image_text", fail)
    result = DocxParser(PdfOcrOptions()).parse(document())
    assert result.success and "Before image" in result.structured_content.full_text()
    assert result.structured_content.metadata["word_embedded_ocr"]["scan_failed"]


def test_reused_image_text_cannot_bypass_output_budget(monkeypatch):
    fake_ocr(monkeypatch)
    monkeypatch.setattr(word_images, "MAX_TEXT_CHARS", len("IMAGE EVIDENCE"))
    result = DocxParser(PdfOcrOptions()).parse(document(duplicate=True))
    assert result.structured_content.full_text().count("IMAGE EVIDENCE") == 1
    assert result.structured_content.metadata["word_embedded_ocr"]["items"][1]["status"] == "budget_exhausted"


def test_actual_image_pipeline_uses_external_and_shares_document_budget(monkeypatch):
    from apps.api.parsers.pdf import PdfParser, _PageOcrResult
    from apps.api.ocr.base import ExternalOcrResult, ExternalOcrLine
    monkeypatch.setattr(PdfParser, "_ocr_page", lambda self, page, *a, **kw: _PageOcrResult(page, []))
    calls = []
    def recognize(png, **kwargs):
        calls.append(png)
        return ExternalOcrResult(lines=[ExternalOcrLine("synthetic external evidence", 950, [0,0,1000,1000])])
    provider = SimpleNamespace(name="synthetic", describe=lambda:{"provider":"synthetic","model":"test"}, recognize=recognize)
    result = DocxParser(PdfOcrOptions(external_provider=provider, external_max_pages=1)).parse(document(duplicate=True, table=True))
    assert result.success and len(calls) == 1
    assert result.structured_content.full_text().count("synthetic external evidence") == 2
    summary = result.structured_content.metadata["word_embedded_ocr"]
    assert summary["items"][0]["sources"] == ["external"]
    assert summary["items"][1]["reused"]
    assert summary["items"][2]["status"] == "failed"
    assert summary["status"] == "partial" and summary["external_calls"] == 1


@pytest.mark.parametrize("suffix", ["docx", "doc", "wps"])
def test_real_mixed_word_conversion_and_local_ocr(tmp_path, suffix):
    import os
    import subprocess
    from PIL import ImageDraw, ImageFont
    if os.environ.get("CANGZHI_TEST_WORD_OCR_INTEGRATION") != "1":
        pytest.skip("requires isolated LibreOffice and Tesseract")
    image = Image.new("RGB", (1200, 300), "white")
    ImageDraw.Draw(image).text((40, 80), "CANGZHI IMAGE EVIDENCE 54321",
        font=ImageFont.truetype("LiberationSans-Regular.ttf", 48), fill="black")
    stream = BytesIO(); image.save(stream, "PNG"); stream.seek(0)
    doc = Document(); doc.add_paragraph("Native before"); doc.add_picture(stream); doc.add_paragraph("Native after")
    source = tmp_path / "synthetic.docx"; doc.save(source)
    if suffix != "docx":
        subprocess.run(["soffice", "-env:UserInstallation="+(tmp_path/"profile").as_uri(),
            "--headless", "--convert-to", "doc:MS Word 97", "--outdir", str(tmp_path), str(source)],
            capture_output=True, check=True, timeout=60)
        source = source.with_suffix(".doc")
    result = get_parser_for_content(None, "synthetic."+suffix,
        pdf_ocr_options=PdfOcrOptions(language="eng",external_max_pages=0)).parse(source.read_bytes())
    assert result.success, result.error_message
    text = result.structured_content.full_text()
    assert text.index("Native before") < text.index("CANGZHI IMAGE EVIDENCE 54321") < text.index("Native after")
    assert result.structured_content.metadata["word_embedded_ocr"]["external_calls"] == 0
