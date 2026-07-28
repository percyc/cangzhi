from functools import lru_cache

from ..core.config import settings
from .base import BlobStorage
from .local import LocalBlobStorage


@lru_cache(maxsize=1)
def get_storage() -> BlobStorage:
    return LocalBlobStorage(settings.storage_path)
