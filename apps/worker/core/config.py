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

    # --- ADR-035 task ownership / lease / recovery.
    # ``task_execution_enabled`` gates the supervised execution path (unique
    # attempt, finite lease + heartbeat, hard subprocess deadline, write
    # fencing, bounded crash recovery). Requires migration 0043 + PostgreSQL.
    # Tests may explicitly disable it to isolate ordinary stage behavior.
    task_execution_enabled: bool = True
    # Finite lease (seconds) the holder must refresh via heartbeat before
    # exceeding; poll/heartbeat interval drifts are bounded by clamps below.
    task_lease_seconds: float = 120.0
    task_heartbeat_seconds: float = 30.0
    # Stage-aware hard deadline (seconds): a supervised child must finish a
    # *single* stage task by this ceiling. Parsing/OCR is sized generously so
    # a legitimate large OCR (up to 300 pages, each with a shared budget) is
    # not rejected: the per-page OCR budget caps external work, and this
    # ceiling only bounds the whole stage. Embedding (one chunk per task) gets
    # a small ceiling so a hung embedding cannot linger.
    task_stage_deadline_seconds: dict = {
        "parsing": 7200.0,
        "stored": 7200.0,
        "embedding": 180.0,
        "chunking": 3600.0,
        "preview": 600.0,
    }
    task_hard_deadline_seconds: float = 3600.0  # fallback for other stages
    # Grace after the hard deadline before a non-cooperative child is
    # SIGKILLed; the child is always joined before the job is requeued.
    task_terminate_grace_seconds: float = 15.0
    # Max recovery_count a job may accumulate via crash recovery.
    # This is separate from the application retry_count.
    task_max_attempts: int = 3
    # Per-operation statement_timeout / lock_timeout (ms) applied to the
    # supervised child and heartbeat engines so a fence verification or
    # heartbeat refresh can never hang on a blocked row lock.
    task_db_timeout_ms: int = 10000


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


# --- ADR-035 task execution policy (clamped).


def _clamp_timeout(value: float, low: float, high: float, default: float) -> float:
    """Clamp seconds to ``[1.0, 86400.0]``, defaulting on unhealthy values."""
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    import math
    if number <= 0 or not math.isfinite(number):
        return default
    return max(low, min(high, number))


def _task_execution_policy() -> dict:
    """Return the validated task-execution policy derived from settings.

    Each value is clamped so a mis-typed environment variable cannot inject a
    zero or negative deadline that would turn every task into an immediate
    timeout or, worse, an unbounded lease. ``db_timeout_ms`` bounds every
    per-operation statement/lock timeout so the fence and heartbeat can never
    hang on a blocked row.
    """
    lease = _clamp_timeout(settings.task_lease_seconds, 1.0, 86400.0, 120.0)
    heartbeat = _clamp_timeout(
        settings.task_heartbeat_seconds, 1.0, 3600.0, 30.0
    )
    hard = _clamp_timeout(
        settings.task_hard_deadline_seconds, 60.0, 86400.0, 3600.0
    )
    grace = _clamp_timeout(
        settings.task_terminate_grace_seconds, 1.0, 300.0, 15.0
    )
    # A heartbeat interval larger than the lease would let the lease expire
    # between refreshes; force it below the clamped lease.
    heartbeat = min(heartbeat, max(lease / 3, 0.1))
    max_attempts = min(10, max(int(settings.task_max_attempts) if settings.task_max_attempts else 3, 1))
    try:
        db_timeout_ms = int(settings.task_db_timeout_ms)
    except (TypeError, ValueError):
        db_timeout_ms = 10000
    db_timeout_ms = min(10000, max(db_timeout_ms, 100))
    return {
        "enabled": bool(settings.task_execution_enabled),
        "lease_seconds": lease,
        "heartbeat_seconds": heartbeat,
        "hard_deadline_seconds": hard,
        "terminate_grace_seconds": grace,
        "max_attempts": max_attempts,
        "db_timeout_ms": db_timeout_ms,
    }


def stage_hard_deadline_seconds(stage: str) -> float:
    """Clamped per-stage hard deadline in seconds.

    Parsing/OCR is sized so a legitimate large OCR (e.g. 300 pages sharing the
    per-page orchestration budget) is not rejected by a too-small ceiling;
    embedding (one chunk per task) is kept small so a hung embedding cannot
    linger. Unknown stages fall back to the general ceiling.
    """
    mapping = getattr(settings, "task_stage_deadline_seconds", None) or {}
    key = "parsing" if stage == "stored" else stage
    raw = mapping.get(key)
    if raw is None:
        raw = settings.task_hard_deadline_seconds
    return _clamp_timeout(raw, 10.0, 86400.0, 3600.0)


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
