from io import BytesIO
import os
from subprocess import CompletedProcess

from PIL import Image, ImageDraw, ImageFont
import pytest

from apps.api.parsers.base import get_parser_for_content
from apps.api.parsers.image import ImageParser
from apps.api.parsers.pdf import PdfOcrOptions, PdfParser
from apps.api.parsers import image as image_module


def image_bytes(fmt="PNG", size=(180, 90), **kwargs):
    output = BytesIO()
    Image.new("RGB", size, "white").save(output, fmt, **kwargs)
    return output.getvalue()


@pytest.mark.parametrize("extension,fmt,mime", [("png", "PNG", "image/png"), ("jpg", "JPEG", "image/jpeg"),
    ("jpeg", "JPEG", "image/jpeg"), ("webp", "WEBP", "image/webp")])
@pytest.mark.parametrize("generic", [False, True])
def test_upload_preserves_image_and_dispatch(client, extension, fmt, mime, generic):
    api, _ = client
    content = image_bytes(fmt)
    response = api.post("/api/files/upload", files={"file": (f"sample.{extension.upper()}", content,
        "application/octet-stream" if generic else mime)})
    assert response.status_code == 201
    document = response.json()
    blob = document["current_version"]["blob"]
    assert blob["content_type"] == mime
    original = api.get(f"/api/documents/{document['id']}/original?inline=true")
    assert original.content == content
    assert original.headers["content-type"] == mime
    assert isinstance(get_parser_for_content(mime, blob["original_filename"]), ImageParser)


@pytest.mark.parametrize("filename,mime", [("a.svg", "image/svg+xml"), ("a.gif", "image/gif"),
    ("a.png", "text/html"), ("a.jpg", "image/png"), ("a.webp", "application/zip")])
def test_reject_image_mismatch(client, filename, mime):
    api, _ = client
    assert api.post("/api/files/upload", files={"file": (filename, b"bad", mime)}).status_code == 415


def fake_ocr(monkeypatch, *, text="Reliable OCR source", confidence=95):
    seen = []
    def runner(png, options):
        seen.append(Image.open(BytesIO(png)).copy())
        tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        tsv += f"5\t1\t1\t1\t1\t1\t10\t10\t40\t20\t{confidence}\t{text}\n"
        return CompletedProcess([], 0, tsv.encode(), b"")
    def factory(options, **kwargs):
        return PdfParser(options, tesseract_runner=runner, tesseract_lookup=lambda: "tesseract", **kwargs)
    monkeypatch.setattr(image_module, "PdfParser", factory)
    return seen


def test_image_exif_coordinates_and_shared_pipeline(monkeypatch):
    seen = fake_ocr(monkeypatch)
    exif = Image.Exif()
    exif[274] = 6
    result = ImageParser(PdfOcrOptions(enabled=True)).parse(image_bytes("JPEG", exif=exif))
    assert result.success
    structured = result.structured_content
    assert structured.document_type == "image"
    assert structured.full_text() == "Reliable OCR source"
    assert seen[0].size == (90, 180)
    assert structured.metadata["image_extraction"]["coordinate_source"] == "exif_transposed_image"
    assert structured.blocks[0].extra["bbox"] == [10.0, 150.0, 50.0, 170.0]
    assert structured.metadata["pdf_extraction"]["ocr_completed_pages"] == [1]


def test_transparency_composited_on_white(monkeypatch):
    seen = fake_ocr(monkeypatch)
    content = BytesIO()
    Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(content, "PNG")
    assert ImageParser(PdfOcrOptions()).parse(content.getvalue()).success
    assert seen[0].getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("budget", [0, 1])
def test_image_external_fallback_obeys_shared_budget(monkeypatch, budget):
    from apps.api.ocr import ExternalOcrLine, ExternalOcrResult
    fake_ocr(monkeypatch, text="", confidence=0)
    class Provider:
        name = "synthetic"
        calls = 0
        def describe(self):
            return {"provider": self.name, "model": "synthetic"}
        def recognize(self, png, *, page_number):
            assert page_number == 1
            assert Image.open(BytesIO(png)).size == (180, 90)
            self.calls += 1
            return ExternalOcrResult(lines=[ExternalOcrLine("External recognized evidence", 950, [0, 0, 500, 500])])
    provider = Provider()
    result = ImageParser(PdfOcrOptions(external_provider=provider, external_max_pages=budget)).parse(image_bytes())
    assert provider.calls == budget
    assert result.success is bool(budget)
    if budget:
        assert result.structured_content.full_text() == "External recognized evidence"
        assert result.structured_content.blocks[0].extra["external_used"] is True


@pytest.mark.parametrize("confidence,text", [(10, "unreliable OCR"), (95, "")])
def test_low_quality_or_empty_not_indexed(monkeypatch, confidence, text):
    fake_ocr(monkeypatch, text=text, confidence=confidence)
    result = ImageParser(PdfOcrOptions()).parse(image_bytes())
    assert not result.success
    assert result.error_details["reason"] == "image_ocr_empty"


def test_format_invalid_disabled_and_pixel_limits(monkeypatch):
    parser = ImageParser(PdfOcrOptions(), expected_format="PNG")
    assert parser.parse(b"<html>not image</html>").error_details["reason"] == "image_invalid"
    assert parser.parse(image_bytes("JPEG")).error_details["reason"] == "image_format_mismatch"
    assert ImageParser().parse(image_bytes()).error_details["reason"] == "image_ocr_disabled"
    monkeypatch.setattr(image_module, "MAX_IMAGE_PIXELS", 100)
    assert parser.parse(image_bytes()).error_details["reason"] == "image_limit_exceeded"


def test_animation_rejected():
    output = BytesIO()
    Image.new("RGB", (30, 30), "red").save(output, "WEBP", save_all=True,
        append_images=[Image.new("RGB", (30, 30), "blue")], duration=100, loop=0)
    assert ImageParser(PdfOcrOptions()).parse(output.getvalue()).error_details["reason"] == "image_multiframe"


def test_webdav_image_import(client, monkeypatch):
    from apps.api.services.webdav import RemoteEntry
    api, _ = client
    content = image_bytes()
    source = api.post("/api/webdav", json={"name": "Image source", "base_url": "https://dav.example.com/",
        "username": "reader", "password": "synthetic", "root_path": "/knowledge"}).json()
    async def propfind(**kwargs):
        return [RemoteEntry(path="/knowledge/scan.png", is_collection=False, etag='"v1"',
            last_modified="today", size=len(content), content_type="application/octet-stream")]
    async def download(**kwargs):
        return content, "application/octet-stream"
    monkeypatch.setattr("apps.api.api.webdav.propfind", propfind)
    monkeypatch.setattr("apps.api.api.webdav.download_file", download)
    monkeypatch.setattr("apps.api.api.documents.download_file", download)
    assert api.post(f"/api/webdav/{source['id']}/scan").status_code == 200
    synced = api.post(f"/api/webdav/{source['id']}/sync")
    assert synced.json()["imported"] == 1
    entry = api.get(f"/api/webdav/{source['id']}/entries").json()[0]
    assert api.get(f"/api/documents/{entry['document_id']}/original").content == content


@pytest.mark.skipif(os.environ.get("CANGZHI_TEST_OCR_ORIENTATION") != "1", reason="requires Tesseract")
def test_real_wide_page_preserves_column_reading_order():
    source = Image.new("RGB", (2000, 1000), "white")
    draw = ImageDraw.Draw(source)
    font = ImageFont.truetype("LiberationSans-Regular.ttf", 32)
    for i in range(8):
        draw.text((50, 50+i*90), f"LEFT column original evidence {i}", font=font, fill="black")
        draw.text((1100, 50+i*90), f"RIGHT column source material {i}", font=font, fill="black")
    output = BytesIO()
    source.save(output, "PNG")
    result = ImageParser(PdfOcrOptions(language="eng")).parse(output.getvalue())
    assert result.success
    text = result.structured_content.full_text()
    assert text.rindex("LEFT") < text.index("RIGHT"), text


@pytest.mark.skipif(os.environ.get("CANGZHI_TEST_OCR_ORIENTATION") != "1", reason="requires Tesseract")
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("language", ["eng", "chi_sim+eng"])
def test_real_landscape_image_all_directions(angle, language):
    source = Image.new("RGB", (1200, 600), "white")
    draw = ImageDraw.Draw(source)
    font = (ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 42)
            if language == "chi_sim+eng" else ImageFont.load_default(size=32))
    lines = (["藏知知识库文档识别", "保留原始资料和来源 12345", "图片文字方向需要正确识别", "识别结果用于全文检索"]
             if language == "chi_sim+eng" else ["CANGZHI DOCUMENT RECOGNITION", "Reliable original evidence 12345",
                "Text direction must be corrected", "Keep sources and coordinates intact"])
    for i, text in enumerate(lines):
        draw.text((50, 60+i*100), text, font=font, fill="black")
    source = source.rotate(angle, expand=True)
    output = BytesIO()
    source.save(output, "PNG")
    result = ImageParser(PdfOcrOptions(enabled=True, language=language)).parse(output.getvalue())
    assert result.success, result.error_message
    assert ("知识库" if language == "chi_sim+eng" else "CANGZHI") in result.structured_content.full_text().replace(" ", "")
    assert "12345" in result.structured_content.full_text()
    for block in result.structured_content.blocks:
        x0, y0, x1, y1 = block.extra["bbox"]
        assert 0 <= x0 < x1 <= source.width
        assert 0 <= y0 < y1 <= source.height
