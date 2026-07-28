import hashlib
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import BinaryIO

from .base import (
    BlobStorage,
    BlobTooLargeError,
    InvalidStorageKeyError,
    StoredBlob,
)

_STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_SIZE = 1024 * 1024


class LocalBlobStorage(BlobStorage):
    def __init__(self, base_path: str | Path):
        self._blob_path = Path(base_path).resolve() / "blobs"
        self._blob_path.mkdir(parents=True, exist_ok=True)

    def _path_for(self, storage_key: str) -> Path:
        if not _STORAGE_KEY_RE.fullmatch(storage_key):
            raise InvalidStorageKeyError("invalid storage key")
        path = (self._blob_path / storage_key).resolve()
        if path.parent != self._blob_path:
            raise InvalidStorageKeyError("storage key escapes blob directory")
        return path

    def save(self, source: BinaryIO, max_bytes: int) -> StoredBlob:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")

        storage_key = secrets.token_hex(32)
        destination = self._path_for(storage_key)
        digest = hashlib.sha256()
        size = 0
        temporary_path: str | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._blob_path,
                prefix=".upload-",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                while chunk := source.read(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise BlobTooLargeError(
                            f"blob exceeds maximum size of {max_bytes} bytes"
                        )
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())

            os.replace(temporary_path, destination)
            temporary_path = None
            return StoredBlob(
                storage_key=storage_key,
                sha256=digest.hexdigest(),
                size=size,
            )
        finally:
            if temporary_path is not None:
                Path(temporary_path).unlink(missing_ok=True)

    def open(self, storage_key: str) -> BinaryIO:
        return self._path_for(storage_key).open("rb")

    def delete(self, storage_key: str) -> None:
        self._path_for(storage_key).unlink(missing_ok=True)

    def exists(self, storage_key: str) -> bool:
        return self._path_for(storage_key).is_file()
