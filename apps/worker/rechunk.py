"""Explicit, bounded maintenance batches; never run automatically at startup.

Run inside the installed Worker container. Preview is the default. Back up and
verify database + storage before --apply --backup-verified. Dataset catalogs are
deliberately excluded: changing document boundaries does not improve row queries.
"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import and_, create_engine, exists, or_, select, text
from sqlalchemy.orm import Session

from apps.api.models.documents import Document, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.workspaces import Workspace
from apps.api.services.workspaces import clear_workspace_context
from apps.worker.services.processor import (
    ASSISTED_CHUNKING_CONFIG, STRUCTURAL_CHUNKING_CONFIG, process_single_job, utc_now,
)

POLICIES = {"assisted-v1": ASSISTED_CHUNKING_CONFIG,
            "structural-v3": STRUCTURAL_CHUNKING_CONFIG}


def _validate_policy(policy):
    if policy not in POLICIES.values():
        raise ValueError("unsupported maintenance policy")


def enqueue_batch(session: Session, *, document_ids=None, limit=10, apply=False,
                  policy=ASSISTED_CHUNKING_CONFIG):
    _validate_policy(policy)
    if not 1 <= limit <= 20:
        raise ValueError("batch limit must be between 1 and 20")
    if apply and session.bind.dialect.name == "postgresql":
        # Serialize maintenance operators; normal Worker job claiming is unchanged.
        session.execute(text("SELECT pg_advisory_xact_lock(72403119)"))
    file_type = DocumentVersion.structured_content["document_type"].as_string()
    busy = exists(select(ProcessingJob.id).where(
        ProcessingJob.document_version_id == DocumentVersion.id,
        or_(ProcessingJob.status == "processing", and_(
            ProcessingJob.stage != "embedding",
            ProcessingJob.status.in_(("created", "retry")),
        )),
    ))
    already = exists(select(ProcessingJob.id).where(
        ProcessingJob.document_version_id == DocumentVersion.id,
        ProcessingJob.stage == "chunking",
        ProcessingJob.config_version == policy,
    ))
    statement = (
        select(Document.id, DocumentVersion.id.label("version_id"), file_type.label("file_type"))
        .join(DocumentVersion, Document.current_version_id == DocumentVersion.id)
        .join(Workspace, Workspace.id == Document.workspace_id)
        .where(Document.is_deleted.is_(False), Workspace.status == "active",
               DocumentVersion.processing_status == "ready",
               file_type.in_(("pdf", "doc", "docx", "markdown", "txt", "html", "note")),
               ~busy, ~already)
        .order_by(Document.id).limit(limit)
    )
    if document_ids is not None:
        if not document_ids or any(type(i) is not int or i <= 0 for i in document_ids):
            raise ValueError("positive document IDs required")
        statement = statement.where(Document.id.in_(document_ids))
    if apply:
        statement = statement.with_for_update(of=Document)
    rows = session.execute(statement).all()
    results = []
    for row in rows:
        if apply:
            session.add(ProcessingJob(
                document_id=row.id, document_version_id=row.version_id,
                stage="chunking", status="created", retry_count=0, max_retries=3,
                config_version=policy,
                idempotency_key=f"{row.version_id}:chunking:{policy}",
            ))
        results.append({"document_id": row.id, "version_id": row.version_id,
                        "file_type": row.file_type})
    return {"applied": apply, "selected": results, "count": len(results),
            "policy": policy}


def run_selected(session: Session, document_ids: list[int], *, max_jobs=100,
                 policy=ASSISTED_CHUNKING_CONFIG):
    """Run only explicit maintenance chunks and their new embeddings.

    Used while the normal Worker is stopped; never consumes the old parse queue.
    Claims one row at a time so an interrupted command leaves no preclaimed batch.
    """
    _validate_policy(policy)
    if not document_ids or any(type(i) is not int or i <= 0 for i in document_ids):
        raise ValueError("positive document IDs required")
    if not 1 <= max_jobs <= 1000:
        raise ValueError("max_jobs must be between 1 and 1000")
    completed = attempted = 0
    for _ in range(max_jobs):
        clear_workspace_context(session)
        now = utc_now()
        job = session.scalar(select(ProcessingJob)
            .join(Document, Document.id == ProcessingJob.document_id)
            .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
            .join(Workspace, Workspace.id == Document.workspace_id)
            .where(Document.id.in_(document_ids), Document.is_deleted.is_(False),
                   Workspace.status == "active",
                   ProcessingJob.document_version_id == DocumentVersion.id,
                   ProcessingJob.status.in_(("created", "retry")),
                   or_(ProcessingJob.next_retry_at.is_(None), ProcessingJob.next_retry_at <= now),
                   or_(and_(ProcessingJob.stage == "chunking",
                            ProcessingJob.config_version == policy),
                       and_(ProcessingJob.stage == "embedding",
                            DocumentVersion.meta["chunk_config_version"].as_string() == policy)))
            .order_by(ProcessingJob.id).limit(1)
            .with_for_update(of=ProcessingJob, skip_locked=True))
        if job is None:
            session.rollback()
            break
        job.status = "processing"
        job.started_at = now
        job.finished_at = None
        job_id = job.id
        session.commit()
        attempted += 1
        if not process_single_job(session, job_id):
            break  # Stop this batch on failure instead of extending impact.
        completed += 1
    return {"attempted": attempted, "completed": completed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument("--ids", type=int, nargs="+")
    targets.add_argument("--all", action="store_true", help="Next eligible batch, not an unbounded enqueue")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-verified", action="store_true")
    parser.add_argument("--run", action="store_true", help="Run only selected new-policy jobs; normal Worker must be stopped")
    parser.add_argument("--max-jobs", type=int, default=100)
    parser.add_argument("--strategy", choices=tuple(POLICIES), default="assisted-v1")
    args = parser.parse_args()
    if args.apply and not args.backup_verified:
        parser.error("verify a fresh database + storage backup before --apply --backup-verified")
    if args.run and (not args.ids or args.apply or not args.backup_verified):
        parser.error("--run requires --ids and --backup-verified, and cannot be combined with --apply")
    from apps.worker.core.config import settings
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        if args.run:
            print(json.dumps(run_selected(session, args.ids, max_jobs=args.max_jobs,
                                          policy=POLICIES[args.strategy])))
            return
        result = enqueue_batch(session, document_ids=args.ids, limit=args.limit, apply=args.apply,
                               policy=POLICIES[args.strategy])
        if args.apply:
            session.commit()
        else:
            session.rollback()
        print(json.dumps(result, ensure_ascii=False))
    engine.dispose()


if __name__ == "__main__":
    main()
