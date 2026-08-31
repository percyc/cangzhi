"""Worker-side tests for the external OCR configuration plumbing.

The worker reads :class:`AIRuntimeConfig` on every parsing job so
a fresh OCR setting is picked up without a process restart. These
tests seed the singleton row with various OCR combinations and
assert that the resulting :class:`PdfOcrOptions` is shaped
correctly. We do not invoke the parser here — that coverage
already lives in :mod:`tests.api.test_pdf_external_ocr`; this
file is only about the worker -> DB -> options bridge.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.core.db import Base
from apps.api.models.auth import Admin, AIRuntimeConfig
from apps.api.parsers.pdf import PdfOcrOptions
from apps.api.security.passwords import hash_password
from apps.api.security.secrets import encrypt_secret
from apps.worker.services.processor import _build_pdf_ocr_options


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_admin(session) -> None:
    hashed = hash_password("a-strong-password")
    session.add(
        Admin(
            username="ocr-worker",
            password_hash=hashed.serialized,
            password_salt=hashed.salt.hex(),
            password_algo=hashed.algo,
            singleton_key="singleton",
        )
    )
    session.flush()


def test_no_runtime_row_falls_back_to_local_only(session):
    _seed_admin(session)
    options = _build_pdf_ocr_options(session)
    assert options.external_provider is None
    assert options.external_max_pages == 20


def test_without_session_keeps_legacy_local_only_path():
    options = _build_pdf_ocr_options()
    assert options.external_provider is None
    assert options.external_max_pages == 20


def test_disabled_ocr_row_yields_no_external_provider(session):
    _seed_admin(session)
    session.add(
        AIRuntimeConfig(
            provider="disabled",
            ocr_provider="disabled",
            timeout_seconds=30,
            prompt_version="v1",
            singleton_key="singleton",
        )
    )
    session.commit()
    options = _build_pdf_ocr_options(session)
    assert options.external_provider is None


def test_openai_ocr_row_builds_provider_with_ciphertext(session):
    _seed_admin(session)
    session.add(
        AIRuntimeConfig(
            provider="disabled",
            ocr_provider="openai",
            ocr_base_url="https://ocr.example.com/v1",
            ocr_model="vision-1",
            ocr_api_key_cipher=encrypt_secret("sk-ocr-worker-key-aaaaaa"),
            has_ocr_api_key=True,
            ocr_timeout_seconds=45,
            ocr_confidence_threshold=750,
            ocr_min_chars=12,
            ocr_max_external_pages=7,
            timeout_seconds=30,
            prompt_version="v1",
            singleton_key="singleton",
        )
    )
    session.commit()

    options = _build_pdf_ocr_options(session)
    assert options.external_provider is not None
    assert options.external_provider._api_key == "sk-ocr-worker-key-aaaaaa"
    assert options.external_provider._base_url == "https://ocr.example.com/v1"
    assert options.external_provider._model == "vision-1"
    assert options.external_provider._timeout == 45
    assert options.external_min_chars == 12
    assert options.external_confidence_threshold == 750
    assert options.external_max_pages == 7


def test_openai_row_without_cipher_yields_no_provider(session):
    _seed_admin(session)
    session.add(
        AIRuntimeConfig(
            provider="disabled",
            ocr_provider="openai",
            ocr_base_url="https://ocr.example.com/v1",
            ocr_model="vision-1",
            ocr_api_key_cipher=None,
            has_ocr_api_key=False,
            timeout_seconds=30,
            prompt_version="v1",
            singleton_key="singleton",
        )
    )
    session.commit()
    options = _build_pdf_ocr_options(session)
    assert options.external_provider is None


def test_options_preserve_local_tesseract_settings(session):
    _seed_admin(session)
    session.add(
        AIRuntimeConfig(
            provider="disabled",
            timeout_seconds=60,
            prompt_version="v1",
            singleton_key="singleton",
        )
    )
    session.commit()
    options = _build_pdf_ocr_options(session)
    assert options.enabled is True
    assert options.language == "chi_sim+eng"
    assert options.dpi == 180
    assert options.min_native_chars == 24
    assert options.max_pages == 300
    assert options.timeout_seconds == 30.0
