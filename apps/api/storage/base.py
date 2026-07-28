from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import BinaryIO


class BlobTooLargeError(ValueError):
    pass


class InvalidStorageKeyError(ValueError):
    pass


@dataclass(frozen=True)
class StoredBlob:
    storage_key: str
    sha256: str
    size: int


class BlobStorage(ABC):
    @abstractmethod
    def save(self, source: BinaryIO, max_bytes: int) -> StoredBlob:
        """Stream a binary source into storage and return its digest and size."""

    @abstractmethod
    def open(self, storage_key: str) -> BinaryIO:
        """Open a stored blob for binary reading."""

    @abstractmethod
    def delete(self, storage_key: str) -> None:
        """Delete a blob if it exists."""

    @abstractmethod
    def exists(self, storage_key: str) -> bool:
        """Return whether a validated key exists."""
