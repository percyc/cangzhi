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
    max_upload_size_mb: int = 50
    log_level: str = "info"

    dataset_query_memory_limit: str = "512MB"
    dataset_query_threads: int = 2
    dataset_query_timeout_seconds: float = 20.0

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

    cors_allowed_origins: str = ""

    # --- Model admission quota pools (ADR-034). All non-secret.
    # Conservative default: ONE shared pool (total 2, background 1) so
    # chat + embedding + OCR talking to a shared upstream never multiply
    # their limits; OCR additionally capped at ``model_admission_max_ocr``.
    # Use ``model_admission_pool_map`` to split into independent pools ONLY
    # for genuinely independent upstreams.
    model_admission_capacity: int = 2
    model_admission_background_capacity: int = 1
    model_admission_max_ocr: int = 1
    model_admission_lease_padding_seconds: float = 60.0
    model_admission_acquire_timeout_seconds: float = 1.5
    model_admission_acquire_poll_seconds: float = 0.05
    model_admission_retry_min_seconds: float = 20.0
    # Optional JSON map purpose -> pool_id; default is one shared pool so
    # different model uses sharing an upstream do not each copy a channel cap.
    model_admission_pool_map: str = ""


settings = Settings()


def ai_settings_summary() -> dict:
    """Return a safe summary of AI settings (no secrets)."""

    return {
        "provider": settings.ai_provider,
        "openai_base_url": settings.openai_base_url,
        "openai_model": settings.openai_model,
        "ollama_base_url": settings.ollama_base_url,
        "ollama_model": settings.ollama_model,
        "has_openai_key": bool(settings.openai_api_key),
        "prompt_version": settings.ai_prompt_version,
    }
