"""Shared secret store for at-rest encryption.

The M3-3 milestone encrypts the OpenAI API key (and any future
secrets) with a Fernet key derived from a single ``CANGZHI_SECRET_KEY``
file living in the shared storage volume. The Worker and the API
processes both mount that volume, so they read the same key.

* If ``CANGZHI_SECRET_KEY`` is set in the environment, that key is
  used directly. The value must be a 32-byte url-safe base64-encoded
  Fernet key.
* Otherwise the manager looks for ``<storage_path>/.secret_key`` and
  lazily generates a new one with 0o600 permissions if the file is
  missing. The path is fixed (no fallback chains) so a misconfigured
  deployment never silently accepts an attacker-supplied key.
* The key is loaded exactly once per process and then cached. Tests
  can call :func:`reset_secret_store` to clear the cache.

The store never tells the caller to delete or rotate the key as a
recovery step: a mismatched key is the kind of failure that should
be escalated to the operator, not silently fixed.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..core.config import settings


_KEY_FILENAME = ".secret_key"
_ENV_KEY = "CANGZHI_SECRET_KEY"
_default_store_lock = threading.Lock()


class SecretStoreError(RuntimeError):
    """Raised when the secret store cannot be initialised."""


class SecretDecryptError(SecretStoreError):
    """Raised when a payload cannot be decrypted with the active key."""


class SecretStore:
    """Filesystem-backed Fernet key store with 0o600 permissions."""

    def __init__(self, storage_path: str | os.PathLike[str] | None) -> None:
        self._lock = threading.Lock()
        if storage_path is None or str(storage_path).strip() == "":
            raise SecretStoreError("未配置 storage 路径，无法持久化主密钥")
        self._storage_path = Path(storage_path).resolve()
        self._key_path = self._storage_path / _KEY_FILENAME
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._fernet: Fernet | None = None
        self._raw_key: bytes | None = None

    @property
    def key_path(self) -> Path:
        return self._key_path

    def _load_or_create_key(self) -> tuple[Fernet, bytes]:
        env_value = os.environ.get(_ENV_KEY)
        if env_value:
            candidate = env_value.strip().encode("utf-8")
            try:
                return Fernet(candidate), candidate
            except (ValueError, TypeError) as exc:
                raise SecretStoreError(
                    "CANGZHI_SECRET_KEY 不是合法的 Fernet 密钥"
                ) from exc
        if self._key_path.exists():
            self._ensure_private_permissions()
            data = self._key_path.read_bytes().strip()
            try:
                return Fernet(data), data
            except (ValueError, TypeError) as exc:
                # The file exists but cannot be parsed. We refuse to
                # touch it: deleting a mismatched key silently is the
                # worst possible recovery path because the previous
                # ciphertext would still be in the database.
                raise SecretStoreError(
                    "存储目录中的主密钥无法读取，请检查文件权限"
                ) from exc
        return self._create_key_file()

    def _ensure_private_permissions(self) -> None:
        """Ensure an on-disk master key is never group/world readable."""

        try:
            mode = self._key_path.stat().st_mode & 0o777
            if mode & 0o077:
                os.chmod(self._key_path, 0o600)
            if self._key_path.stat().st_mode & 0o077:
                raise SecretStoreError("主密钥文件权限不安全，必须仅允许当前用户读取")
        except OSError as exc:
            raise SecretStoreError("无法检查主密钥文件权限，请联系管理员") from exc

    def _create_key_file(self) -> tuple[Fernet, bytes]:
        new_key = Fernet.generate_key()
        # Atomic write: O_CREAT | O_EXCL so we never clobber an
        # existing key (e.g. created by a sibling process that beat
        # us to the punch). If a concurrent process wins, we fall
        # back to reading the file they just created.
        if self._key_path.exists():
            self._ensure_private_permissions()
            data = self._key_path.read_bytes().strip()
            try:
                return Fernet(data), data
            except (ValueError, TypeError):
                raise SecretStoreError(
                    "主密钥文件存在但格式错误，请联系管理员"
                ) from None
        try:
            fd = os.open(
                str(self._key_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            self._ensure_private_permissions()
            data = self._key_path.read_bytes().strip()
            try:
                return Fernet(data), data
            except (ValueError, TypeError):
                raise SecretStoreError(
                    "主密钥文件存在但格式错误，请联系管理员"
                ) from None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(new_key)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.unlink(self._key_path)
            except FileNotFoundError:
                pass
            raise
        self._ensure_private_permissions()
        return Fernet(new_key), new_key

    def _fernet_instance(self) -> Fernet:
        with self._lock:
            if self._fernet is None:
                fernet, raw = self._load_or_create_key()
                self._fernet = fernet
                self._raw_key = raw
            return self._fernet

    def _raw_key_bytes(self) -> bytes:
        with self._lock:
            if self._raw_key is None:
                _, raw = self._load_or_create_key()
                self._fernet = self._fernet or Fernet(raw)
                self._raw_key = raw
            return self._raw_key

    def encrypt(self, value: str) -> str:
        if value is None:
            return ""
        token = self._fernet_instance().encrypt(value.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            plaintext = self._fernet_instance().decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise SecretDecryptError("主密钥不匹配或密文已损坏") from exc
        return plaintext.decode("utf-8")

    def key_fingerprint(self) -> str:
        """Return a short, non-secret fingerprint of the active key.

        The fingerprint is the first 6 bytes of the SHA-256 of the
        raw Fernet key, formatted as hex with a leading ``fk_`` so
        the audit log can refer to "fk_a1b2c3…" without exposing the
        key itself.
        """

        raw = self._raw_key_bytes()
        digest = hashlib.sha256(raw).hexdigest()[:12]
        return f"fk_{digest}"


_default_store: SecretStore | None = None


def get_secret_store() -> SecretStore:
    """Return the process-wide :class:`SecretStore`.

    The first call resolves the storage path from settings; later
    calls reuse the same instance.
    """

    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = SecretStore(settings.storage_path)
    return _default_store


def reset_secret_store() -> None:
    """Forget the cached :class:`SecretStore`.

    Tests use this to swap in a temporary storage path; production
    code should never call it.
    """

    global _default_store
    with _default_store_lock:
        _default_store = None


def encrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    return get_secret_store().encrypt(value)


def decrypt_secret(token: str | None) -> str:
    if not token:
        return ""
    return get_secret_store().decrypt(token)
