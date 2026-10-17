"""A synthetic image probe using the same provider contract as ingestion."""
from io import BytesIO
import re

from PIL import Image, ImageDraw, ImageFont

from ..ocr import ExternalOcrError, OpenAICompatibleOcrProvider


def probe_ocr(*, base_url: str, model: str, api_key: str, timeout: float) -> dict:
    # No private document or expected answer is included in the prompt.
    expected = "CANGZHI 5827"
    image = Image.new("RGB", (420, 100), "white")
    ImageDraw.Draw(image).text((20, 25), expected, font=ImageFont.load_default(size=40), fill="black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    provider = OpenAICompatibleOcrProvider(
        base_url=base_url, model=model, api_key=api_key, timeout=timeout,
    )
    try:
        result = provider.recognize(buffer.getvalue(), page_number=1)
    except ExternalOcrError as exc:
        messages = {
            "auth": "鉴权失败，请检查密钥与模型权限。",
            "timeout": "实际图片识别请求超时，请检查渠道响应速度或调整 OCR 超时。",
            "network": "无法连接图片识别接口，请检查网络与 API 地址。",
            "invalid": "接口返回格式不符合 OCR 要求，请确认模型支持图片及结构化识别输出。",
            "config": "OCR 配置不完整，请检查地址、密钥与模型。",
            "upstream": "上游拒绝或无法完成图片识别，请检查模型名称、图片支持、权限与服务状态。",
        }
        category = exc.category if exc.category in messages else "upstream"
        return {"ok": False, "category": category, "message": messages[category]}
    except Exception:
        return {"ok": False, "category": "internal", "message": "OCR 测试异常，请检查服务日志；未返回上游响应内容。"}
    actual = re.sub(r"\s+", "", "".join(line.text for line in result.lines)).upper()
    if "CANGZHI5827" not in actual:
        return {"ok": False, "category": "recognition", "message": "接口已响应，但未正确识别合成测试图文字；请检查视觉识别能力。"}
    return {"ok": True, "message": "合成图片识别通过；不代表复杂扫描件、中文表格或整份文档质量已验证。"}
