import pytest
from apps.api.ocr import ExternalOcrError, ExternalOcrLine, ExternalOcrResult
from apps.api.services.ocr_probe import probe_ocr


@pytest.mark.parametrize("category", ["auth", "timeout", "network", "invalid", "config", "upstream", "unknown"])
def test_probe_sanitizes_errors(monkeypatch, category):
    def fail(*args, **kwargs):
        raise ExternalOcrError("SECRET upstream body", category=category)
    monkeypatch.setattr("apps.api.services.ocr_probe.OpenAICompatibleOcrProvider.recognize", fail)
    result = probe_ocr(base_url="https://example.test/v1", model="alias", api_key="secret", timeout=30)
    assert not result["ok"]
    assert "SECRET" not in str(result)
    assert result["category"] == (category if category != "unknown" else "upstream")


@pytest.mark.parametrize("text,ok", [("CANGZHI 5827", True), ("cangzhi\n5827", True), ("", False), ("unrelated", False)])
def test_probe_checks_image_content(monkeypatch, text, ok):
    def recognize(self, png, *, page_number):
        assert png.startswith(b"\x89PNG")
        assert page_number == 1
        return ExternalOcrResult(lines=[ExternalOcrLine(text=text)])
    monkeypatch.setattr("apps.api.services.ocr_probe.OpenAICompatibleOcrProvider.recognize", recognize)
    assert probe_ocr(base_url="https://example.test/v1", model="alias", api_key="secret", timeout=30)["ok"] is ok
