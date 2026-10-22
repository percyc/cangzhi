"""Durable, leased WebDAV scan batches. No downloads or model calls."""
import asyncio
import hashlib
import json
import posixpath
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy import delete, or_, select

from ..models.webdav import WebDAVScan, WebDAVScanFile, WebDAVSource
from ..models.workspaces import Workspace
from ..security.secrets import decrypt_secret
from ..security.url_safety import URLSecurityError
from .webdav import (
    RemoteEntry, WebDAVError, matches_ignore_pattern, normalized_remote_path,
    propfind, scan_root_path,
)
from .webdav_reconciliation import reconcile_scan
from .workspaces import bind_workspace_context

ACTIVE = ("queued", "running")
LEASE_SECONDS = 120
MAX_DIRECTORIES = 2000
MAX_ENTRIES = 100000


class ScanConflict(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def config_hash(source):
    config = [getattr(source, key) for key in (
        "base_url", "root_path", "username", "password_cipher", "recursive",
        "trusted_private_network", "include_extensions", "ignore_patterns",
        "remote_delete_policy", "is_enabled",
    )]
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def public_scan(run):
    if run is None:
        return None
    end = run.finished_at or utcnow()
    return {
        "id": run.id, "status": run.status, "scanned_directories": run.scanned,
        "pending_directories": len(run.pending), "discovered_files": run.files,
        "elapsed_seconds": max(0, int((aware(end) - aware(run.created_at)).total_seconds())),
        "error": run.error, "resumed": run.resumed, "result": run.result,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


async def lock_source(db, source_id):
    return await db.scalar(select(WebDAVSource).where(WebDAVSource.id == source_id)
                           .with_for_update().execution_options(populate_existing=True))


async def current_scan(db, source_id, *, lock=False):
    query = select(WebDAVScan).where(WebDAVScan.source_id == source_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(query)


async def ensure_not_scanning(db, source_id):
    # All writers acquire source before scan, including worker finalization.
    await lock_source(db, source_id)
    run = await current_scan(db, source_id, lock=True)
    if run and run.status in ACTIVE:
        raise ScanConflict("扫描正在后台执行，请先取消扫描再进行此操作")


async def start_scan(db, source_id, *, resume=False):
    source = await lock_source(db, source_id)
    if source is None or not source.is_enabled:
        raise ScanConflict("知识源不存在或已停用")
    if source.sync_status == "syncing":
        raise ScanConflict("正在同步入库，请等待完成后扫描")
    run = await current_scan(db, source_id, lock=True)
    if run and run.status in ACTIVE:
        return public_scan(run)  # Idempotent double click.
    if source.sync_status == "scanning":
        raise ScanConflict("兼容接口正在扫描，请等待完成后重试")
    fingerprint = config_hash(source)
    if resume:
        if run is None or run.status not in ("failed", "cancelled"):
            raise ScanConflict("没有可继续的扫描，请重新扫描")
        if run.config_hash != fingerprint:
            raise ScanConflict("来源配置已变化，请重新扫描")
        run.status, run.error, run.finished_at = "queued", None, None
        run.resumed = True
        run.lease_token = run.lease_until = None
    else:
        if run:
            await db.execute(delete(WebDAVScanFile).where(WebDAVScanFile.scan_id == run.id))
            await db.delete(run)
            await db.flush()
        root = scan_root_path(source.base_url, source.root_path)
        run = WebDAVScan(source_id=source.id, status="queued", config_hash=fingerprint,
                         root=root, pending=[root], visited=[root], scanned=0,
                         files=0, entries=0, resumed=False)
        db.add(run)
    source.sync_status, source.last_error = "scanning", None
    await db.commit()
    await db.refresh(run)
    return public_scan(run)


async def cancel_scan(db, source_id):
    source = await lock_source(db, source_id)
    run = await current_scan(db, source_id, lock=True)
    if run and run.status in ACTIVE:
        run.status, run.finished_at = "cancelled", utcnow()
        run.lease_token = run.lease_until = None
        source.sync_status = "idle"
        await db.commit()
        await db.refresh(run)
    return public_scan(run)


async def _owned(db, source_id, run_id, token):
    source = await lock_source(db, source_id)
    run = await current_scan(db, source_id, lock=True)
    if (source is None or run is None or run.id != run_id
            or run.status != "running" or run.lease_token != token):
        return None, None
    return source, run


async def process_scan_batch(session_factory, *, client=None, list_directory=None):
    """Claim at most three directories; release DB locks during network IO."""
    list_directory = list_directory or propfind
    now, token = utcnow(), uuid4().hex
    async with session_factory() as db:
        candidate = await db.scalar(select(WebDAVScan).where(
            WebDAVScan.status.in_(ACTIVE),
            or_(WebDAVScan.lease_until.is_(None), WebDAVScan.lease_until < now),
        ).order_by(WebDAVScan.updated_at, WebDAVScan.id).limit(1))
        if candidate is None:
            return False
        source = await lock_source(db, candidate.source_id)
        run = await current_scan(db, candidate.source_id, lock=True)
        if source is None or run is None or run.status not in ACTIVE:
            return False
        if run.lease_until and aware(run.lease_until) >= now:
            return False
        workspace = await db.get(Workspace, source.workspace_id)
        if (not source.is_enabled or not workspace or workspace.status != "active"
                or run.config_hash != config_hash(source)):
            run.status, run.error = "failed", "来源或工作空间已变化，请重新扫描"
            run.finished_at = now
            source.sync_status, source.last_error = "failed", run.error
            await db.commit()
            return True
        if run.lease_token:
            run.resumed = True  # crashed worker / expired lease
        try:
            password = decrypt_secret(source.password_cipher)
        except Exception:
            run.status, run.error = "failed", "来源凭据无法解密，请重新保存连接配置并重新扫描"
            run.finished_at = now
            run.lease_token = run.lease_until = None
            source.sync_status, source.last_error = "failed", run.error
            await db.commit()
            return True
        run.status, run.lease_token = "running", token
        run.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        batch = list(run.pending[:3])
        source_id, run_id = source.id, run.id
        connection = dict(base_url=source.base_url, username=source.username,
                          password=password,
                          trusted_private_network=source.trusted_private_network)
        await db.commit()

    try:
        async def fetch(directory):
            try:
                async with asyncio.timeout(30):
                    return await list_directory(**connection, root_path=directory,
                                                depth="1", absolute_path=True, client=client)
            except (WebDAVError, URLSecurityError, TimeoutError) as exc:
                return exc
            except Exception:
                return WebDAVError("目录请求失败，已保留进度，请检查连接后继续")
        responses = await asyncio.gather(*(fetch(path) for path in batch))
        async with session_factory() as db:
            source, run = await _owned(db, source_id, run_id, token)
            if run is None:
                return True
            if run.config_hash != config_hash(source):
                raise WebDAVError("来源配置已变化，请重新扫描")
            workspace = await db.get(Workspace, source.workspace_id)
            if not workspace or workspace.status != "active":
                raise WebDAVError("工作空间已归档，请恢复后重新扫描")
            bind_workspace_context(db.sync_session, source.workspace_id)
            visited, pending = set(run.visited), list(run.pending)
            failure = None
            for directory, response in zip(batch, responses):
                if isinstance(response, BaseException):
                    failure = (str(response) if isinstance(response, WebDAVError)
                               else "目录连接超时或地址校验失败，请检查后继续")
                    continue
                run.entries += len(response)
                if run.entries > MAX_ENTRIES:
                    raise WebDAVError("条目超过10万，请缩小来源目录并重新扫描")
                found = {}
                for entry in response:
                    path = normalized_remote_path(entry.path)
                    if path == directory:
                        continue
                    if posixpath.dirname(path) != directory or len(path) > 2048:
                        raise WebDAVError("目录返回越界或过长路径，请检查来源")
                    if matches_ignore_pattern(path, source.ignore_patterns or []):
                        continue
                    if entry.is_collection:
                        if source.recursive and path not in visited:
                            visited.add(path)
                            if len(visited) > MAX_DIRECTORIES:
                                raise WebDAVError("目录超过2000个，请缩小来源目录并重新扫描")
                            pending.append(path)
                    else:
                        data = asdict(entry)
                        data["path"] = path
                        found[path] = data
                db.add_all([WebDAVScanFile(scan_id=run.id, path=path, data=data)
                            for path, data in found.items()])
                run.files += len(found)
                run.scanned += 1
                pending.remove(directory)
            run.pending, run.visited = pending, sorted(visited)
            run.lease_token = run.lease_until = None
            if failure:
                run.status, run.error, run.finished_at = "failed", failure, utcnow()
                source.sync_status, source.last_error = "failed", failure
            elif not pending:
                await db.flush()
                rows = (await db.scalars(select(WebDAVScanFile.data).where(
                    WebDAVScanFile.scan_id == run.id))).all()
                confirm_missing = not run.resumed and utcnow() - aware(run.created_at) < timedelta(hours=24)
                run.result = await reconcile_scan(
                    db, source, [RemoteEntry(**row) for row in rows], run.root, run.scanned,
                    extensions=set(source.include_extensions or []),
                    confirm_missing=confirm_missing, commit=False,
                )
                run.result = {**run.result, "missing_check_performed": confirm_missing}
                run.status, run.finished_at = "completed", utcnow()
                run.error = None
                source.last_error = None
                await db.execute(delete(WebDAVScanFile).where(WebDAVScanFile.scan_id == run.id))
            else:
                run.status = "queued"
            await db.commit()
    except Exception as exc:
        # Never expose raw upstream/DB errors containing credentials or file content.
        async with session_factory() as db:
            source, run = await _owned(db, source_id, run_id, token)
            if run:
                message = str(exc) if isinstance(exc, WebDAVError) else "扫描批次失败，已保留进度，请重试或重新扫描"
                run.status, run.error, run.finished_at = "failed", message, utcnow()
                run.lease_token = run.lease_until = None
                source.sync_status, source.last_error = "failed", message
                await db.commit()
    return True
