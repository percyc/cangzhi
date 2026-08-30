from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://cangzhi:cangzhi-dev@localhost:5432/cangzhi"
    storage_path: str = "./storage"
    poll_interval: int = 5
    log_level: str = "info"
    dataset_query_memory_limit: str = "512MB"
    dataset_query_threads: int = 2

    ai_provider: Literal["", "openai", "ollama"] = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    ai_request_timeout_seconds: float = 30.0
    ai_prompt_version: str = "v1"

    url_fetch_max_bytes: int = 5 * 1024 * 1024
    url_fetch_timeout_seconds: float = 20.0
    url_fetch_max_redirects: int = 5

    pdf_ocr_enabled: bool = True
    pdf_ocr_language: str = "chi_sim+eng"
    pdf_ocr_dpi: int = 180
    pdf_ocr_min_native_chars: int = 24
    pdf_ocr_max_pages: int = 300
    pdf_ocr_timeout_seconds: float = 30.0


settings = Settings()


def _clamp_int(value: int, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: float, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def pdf_ocr_options_summary() -> dict:
    return {
        "enabled": bool(settings.pdf_ocr_enabled),
        "language": (settings.pdf_ocr_language or "chi_sim+eng").strip() or "chi_sim+eng",
        "dpi": _clamp_int(settings.pdf_ocr_dpi, 72, 600, 180),
        "min_native_chars": _clamp_int(settings.pdf_ocr_min_native_chars, 1, 10_000, 24),
        "max_pages": _clamp_int(settings.pdf_ocr_max_pages, 1, 50_000, 300),
        "timeout_seconds": _clamp_float(
            settings.pdf_ocr_timeout_seconds, 1.0, 600.0, 30.0
        ),
    }


def ai_settings_summary() -> dict:
    return {
        "provider": settings.ai_provider,
        "openai_base_url": settings.openai_base_url,
        "openai_model": settings.openai_model,
        "ollama_base_url": settings.ollama_base_url,
        "ollama_model": settings.ollama_model,
        "has_openai_key": bool(settings.openai_api_key),
        "prompt_version": settings.ai_prompt_version,
    }
