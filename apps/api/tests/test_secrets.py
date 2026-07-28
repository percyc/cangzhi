"""Unit tests for the Fernet-backed secret store."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from apps.api.security.secrets import (
    SecretDecryptError,
    SecretStore,
    SecretStoreError,
    decrypt_secret,
    encrypt_secret,
    get_secret_store,
    reset_secret_store,
)


@pytest.fixture
def storage_path(tmp_path, monkeypatch):
    """Point the SecretStore at a temporary directory."""

    path = tmp_path / "storage"
    path.mkdir()
    monkeypatch.setenv("CANGZHI_SECRET_KEY", "")
    # ``settings`` is a module-level instance, so we update it in
    # place to keep the same object but point ``storage_path`` at
    # the temporary directory for the duration of the test.
    from apps.api.core.config import settings as app_settings

    original_storage_path = app_settings.storage_path
    app_settings.storage_path = str(path)
    reset_secret_store()
    try:
        yield path
    finally:
        app_settings.storage_path = original_storage_path
        reset_secret_store()


def test_secret_store_creates_key_file_with_safe_permissions(storage_path):
    store = SecretStore(storage_path)
    store.encrypt("hello")
    key_path = storage_path / ".secret_key"
    assert key_path.exists()
    mode = key_path.stat().st_mode & 0o777
    # On most systems the umask is honoured and we get exactly 0o600;
    # on a system that ignored the mode argument we at least get
    # 0o600 in the dirent so 0o600-or-stricter is acceptable.
    assert mode & 0o077 == 0


def test_secret_store_reuses_existing_key(storage_path):
    store_a = SecretStore(storage_path)
    token = store_a.encrypt("ping")
    key_bytes = (storage_path / ".secret_key").read_bytes()
    store_b = SecretStore(storage_path)
    assert store_b.decrypt(token) == "ping"
    # The same key bytes must be on disk so the Worker and the API
    # agree.
    assert (storage_path / ".secret_key").read_bytes() == key_bytes


def test_secret_store_repairs_insecure_existing_permissions(storage_path):
    key_path = storage_path / ".secret_key"
    key_path.write_bytes(Fernet.generate_key())
    key_path.chmod(0o644)
    SecretStore(storage_path).encrypt("hello")
    assert key_path.stat().st_mode & 0o077 == 0


def test_secret_store_handles_concurrent_creation(storage_path):
    """Two threads racing to create the key must both succeed.

    The store uses ``O_EXCL`` and falls back to reading the file
    the other thread just wrote. Fernet tokens are non-deterministic
    (random IV) so we cannot compare tokens directly; instead we
    decrypt every token and assert they all map to the same
    plaintext and live behind the same on-disk key.
    """

    tokens: list[str] = []
    errors: list[Exception] = []

    def worker():
        try:
            tokens.append(get_secret_store().encrypt("shared"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(tokens) == 8
    # Fernet nonces vary, so we decrypt every token to verify they
    # all use the same on-disk key.
    store = get_secret_store()
    for token in tokens:
        assert store.decrypt(token) == "shared"
    assert (storage_path / ".secret_key").stat().st_mode & 0o077 == 0


def test_secret_store_rejects_invalid_env_key(storage_path, monkeypatch):
    monkeypatch.setenv("CANGZHI_SECRET_KEY", "not-a-fernet-key")
    reset_secret_store()
    with pytest.raises(SecretStoreError):
        SecretStore(storage_path).encrypt("hi")


def test_secret_store_uses_env_key_when_provided(storage_path, monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("CANGZHI_SECRET_KEY", key)
    reset_secret_store()
    token = encrypt_secret("hi")
    # The on-disk key file should not be created when the env var
    # already provides a key.
    assert not (storage_path / ".secret_key").exists()
    assert decrypt_secret(token) == "hi"


def test_secret_store_decrypt_rejects_mismatched_key(storage_path, monkeypatch):
    store = SecretStore(storage_path)
    token = store.encrypt("secret")
    other_key = Fernet(Fernet.generate_key())
    forged = other_key.encrypt(b"secret").decode("ascii")
    with pytest.raises(SecretDecryptError):
        store.decrypt(forged)
    # The good token still decrypts; the failure is specific to the
    # bad ciphertext.
    assert store.decrypt(token) == "secret"


def test_secret_store_does_not_suggest_deleting_key_on_mismatch(storage_path):
    store = SecretStore(storage_path)
    store.encrypt("ok")
    with pytest.raises(SecretDecryptError) as exc_info:
        store.decrypt(Fernet(Fernet.generate_key()).encrypt(b"hi").decode("ascii"))
    message = str(exc_info.value)
    # The recovery hint must not point the operator at deleting the
    # on-disk master key; doing so would lose every previously
    # encrypted secret.
    assert "删除" not in message
    assert "rotate" not in message.lower()
    assert "delete" not in message.lower()


def test_secret_store_fingerprint_is_stable(storage_path):
    store = SecretStore(storage_path)
    store.encrypt("anything")
    fp_a = store.key_fingerprint()
    fp_b = store.key_fingerprint()
    assert fp_a == fp_b
    assert fp_a.startswith("fk_")
    assert len(fp_a) == len("fk_") + 12
