from io import BytesIO

import pytest

from apps.api.storage.base import BlobTooLargeError, InvalidStorageKeyError
from apps.api.storage.local import LocalBlobStorage


def test_local_storage_streams_and_reads_content(tmp_path):
    storage = LocalBlobStorage(tmp_path)
    stored = storage.save(BytesIO(b"hello cangzhi"), max_bytes=1024)

    assert stored.size == 13
    assert len(stored.sha256) == 64
    assert len(stored.storage_key) == 64
    assert storage.exists(stored.storage_key)
    with storage.open(stored.storage_key) as stream:
        assert stream.read() == b"hello cangzhi"

    storage.delete(stored.storage_key)
    assert not storage.exists(stored.storage_key)


def test_local_storage_removes_partial_file_when_too_large(tmp_path):
    storage = LocalBlobStorage(tmp_path)

    with pytest.raises(BlobTooLargeError):
        storage.save(BytesIO(b"too large"), max_bytes=3)

    assert list((tmp_path / "blobs").iterdir()) == []


@pytest.mark.parametrize(
    "key",
    ["../outside", "/tmp/outside", "a" * 63, "g" * 64, ""],
)
def test_local_storage_rejects_untrusted_keys(tmp_path, key):
    storage = LocalBlobStorage(tmp_path)

    with pytest.raises(InvalidStorageKeyError):
        storage.exists(key)
