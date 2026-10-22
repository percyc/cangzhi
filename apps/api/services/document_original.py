"""Read a WebDAV original without confusing remote changes with a saved version."""
from dataclasses import dataclass
from hashlib import sha256
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.documents import Document, DocumentVersion
from ..models.webdav import WebDAVEntry, WebDAVSource
from ..security.secrets import SecretDecryptError, SecretStoreError, decrypt_secret
from ..security.url_safety import URLSecurityError
from .webdav import WebDAVError


class OriginalReadError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class RemoteOriginal:
    content: bytes
    filename: str
    content_type: str


async def read_webdav_original(
    db: AsyncSession,
    document: Document,
    version: DocumentVersion,
    *,
    pinned: bool,
    max_bytes: int,
    downloader: Callable[..., Awaitable[tuple[bytes, str]]],
) -> RemoteOriginal:
    """Fetch on demand; a version-pinned response must match the stored bytes.

    WebDAV originals are temporary during parsing. A missing local blob is
    therefore expected, not proof the remote original disappeared. No files,
    document versions or parsing jobs are written by this operation.
    """
    source_id = (document.meta or {}).get("webdav_source_id")
    source = await db.scalar(
        select(WebDAVSource).where(
            WebDAVSource.id == source_id,
            WebDAVSource.workspace_id == document.workspace_id,
        )
    ) if type(source_id) is int else None
    entry = await db.scalar(
        select(WebDAVEntry).where(
            WebDAVEntry.document_id == document.id,
            WebDAVEntry.source_id == source_id,
        )
    ) if source is not None else None
    if source is None or entry is None:
        raise OriginalReadError(409, "原 WebDAV 连接器或文件记录已不存在，无法获取原文件")
    filename = entry.remote_path.rsplit("/", 1)[-1]
    expected_hash = version.content_hash
    try:
        payload, upstream_type = await downloader(
            base_url=source.base_url,
            remote_path=entry.remote_path,
            username=source.username,
            password=decrypt_secret(source.password_cipher),
            trusted_private_network=source.trusted_private_network,
            max_bytes=max_bytes,
        )
    except (SecretDecryptError, SecretStoreError):
        raise OriginalReadError(502, "无法读取知识源凭据，请检查知识源配置") from None
    except (WebDAVError, URLSecurityError):
        # Do not echo upstream URLs, credential-bearing errors or file paths.
        raise OriginalReadError(502, "获取 WebDAV 原文件失败，请检查知识源连接或远端文件状态") from None
    if pinned and sha256(payload).hexdigest() != expected_hash:
        raise OriginalReadError(
            409,
            "远端原文件已变化，无法提供所请求版本；请同步最新版本，或查看已保存的解析正文",
        )
    return RemoteOriginal(payload, filename, upstream_type.split(";", 1)[0].strip())
