from io import BytesIO
from PIL import Image
import pytest
from apps.api.parsers.pdf import _PageRender, _PageOcrResult
from apps.api.parsers.ocr_orientation import correct_orientation, restore_boxes


@pytest.mark.parametrize("angle,expected", [(90,[20,140,40,170]),(180,[40,160,70,180]),(270,[60,30,80,60])])
def test_inverse_rotation_bbox(angle, expected):
    result = restore_boxes([{"bbox":[30,20,60,40],"text":"test"}], angle, 100, 200)
    assert result[0]["bbox"] == expected
    assert result[0]["rotation_correction"] == angle


def sample_render():
    buffer = BytesIO()
    Image.new("RGB", (100,200), "white").save(buffer, "PNG")
    return _PageRender(100,200,100,200,buffer.getvalue())


def test_confident_page_never_retried():
    original = _PageOcrResult(1,[{"text":"already good", "confidence":0.9}])
    result, _, angle = correct_orientation(sample_render(), original,
        lambda *args: pytest.fail("must not rerun"), budget_seconds=30)
    assert result is original and angle == 0


def test_direction_trials_bounded_and_coordinates_restored():
    calls=[]
    def recognize(render, timeout):
        calls.append(timeout)
        return _PageOcrResult(1,[{"text":"clear synthetic words", "confidence":0.9 if len(calls)==1 else 0.2, "bbox":[30,20,60,40]}])
    result, render, angle = correct_orientation(sample_render(),
        _PageOcrResult(1,[{"text":"bad", "confidence":0.1}]), recognize, budget_seconds=30)
    assert len(calls)==3 and all(0 < n <= 30 for n in calls)
    assert angle==90 and (render.width_px,render.height_px)==(200,100)
    assert result.lines[0]["bbox"]==[20,140,40,170]


def test_budget_exhaustion_leaves_original():
    original = _PageOcrResult(1,[])
    result, _, angle = correct_orientation(sample_render(), original,
        lambda *args: pytest.fail("budget exhausted"), budget_seconds=0)
    assert result is original and angle == 0


def test_real_mixed_direction_scanned_pdf():
    import os
    if os.environ.get("CANGZHI_TEST_OCR_ORIENTATION") != "1":
        pytest.skip("requires isolated Tesseract runtime")
    from PIL import ImageDraw, ImageFont
    from apps.api.parsers.pdf import PdfParser, PdfOcrOptions
    image = Image.new("RGB", (1200,600), "white")
    draw=ImageDraw.Draw(image)
    font=ImageFont.load_default(size=32)
    for y,line in enumerate(["CANGZHI DOCUMENT RECOGNITION", "Reliable original evidence 12345", "Text direction must be corrected", "Keep sources and coordinates intact"]):
        draw.text((50,60+y*100),line,font=font,fill="black")
    pages=[image.rotate(angle,expand=True) for angle in (0,90,180,270)]
    output=BytesIO()
    pages[0].save(output,"PDF",save_all=True,append_images=pages[1:])
    parsed=PdfParser(PdfOcrOptions(enabled=True,language="eng")).parse(output.getvalue())
    assert parsed.success
    for page in range(1,5):
        blocks=[b for b in parsed.structured_content.blocks if b.page==page]
        assert "CANGZHI" in " ".join(b.text for b in blocks)
        assert all(b.extra["bbox"][0]>=0 and b.extra["bbox"][1]>=0 for b in blocks)
