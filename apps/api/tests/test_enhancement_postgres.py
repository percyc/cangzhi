"""Opt-in concurrency check on an isolated, migrated PostgreSQL test database.

Never points at application DATABASE_URL. The runner must explicitly provide
CANGZHI_TEST_POSTGRES_URL with database name cangzhi_enhancement_test.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.workspaces import Workspace
from apps.api.services import knowledge_enhancement as service
from apps.api.services.workspaces import bind_workspace_context
from apps.worker.services import enhancement_processor as worker


@pytest.mark.skipif(not os.environ.get('CANGZHI_TEST_POSTGRES_URL'), reason='isolated PostgreSQL opt-in')
def test_postgres_start_and_execution_are_serialized(monkeypatch):
    url = os.environ['CANGZHI_TEST_POSTGRES_URL']
    assert make_url(url).database == 'cangzhi_enhancement_test'
    engine = create_engine(url)
    with Session(engine) as db:
        space = Workspace(slug='enhancement-' + uuid4().hex, name='Test')
        db.add(space)
        db.flush()
        space_id = space.id
        bind_workspace_context(db, space_id)
        service.save_settings(db, {'enabled': True, 'modules': ['chapter'],
                                  'call_budget': 2, 'cost_acknowledged': True})
        doc = Document(title='Concurrency sample', source_type=DocumentSourceType.note, workspace_id=space_id)
        db.add(doc)
        db.flush()
        version = DocumentVersion(document_id=doc.id, version_number=1, content_hash='test',
            processing_status='ready', structured_content={'document_type': 'note', 'blocks': [
                {'type': 'paragraph', 'text': 'Facts', 'heading_path': []}]})
        db.add(version)
        db.flush()
        doc.current_version_id = version.id
        doc_id = doc.id
        db.commit()
    barrier = Barrier(2)
    def create():
        with Session(engine) as db:
            bind_workspace_context(db, space_id)
            barrier.wait(timeout=10)
            result = service.start_run(db, doc_id, cost_acknowledged=True)
            db.commit()
            return result['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        ids = [future.result(timeout=15) for future in futures]
    assert ids[0] == ids[1]
    with Session(engine) as db:
        jobs = db.scalars(select(ProcessingJob).where(ProcessingJob.document_id == doc_id)).all()
        assert len(jobs) == 1
        job_id = jobs[0].id
    entered, release = Event(), Event()
    calls = []
    class FakeProvider:
        name = 'fake'
        def generate_json(self, **kwargs):
            calls.append(True)
            entered.set()
            assert release.wait(timeout=10)
            return {'summary': {'text': 'Facts', 'evidence_ids': [0]},
                    'entities': [], 'relations': [], 'events': []}
    monkeypatch.setattr(worker, 'build_provider_from_session', lambda _: FakeProvider())
    def execute():
        with Session(engine) as db:
            bind_workspace_context(db, space_id)
            return worker.process_enhancement_job(db, db.get(ProcessingJob, job_id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute)
        try:
            assert entered.wait(timeout=10)
            assert pool.submit(execute).result(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=10)
    with Session(engine) as db:
        bind_workspace_context(db, space_id)
        result = service.read_run(db, ids[0])['run']
        assert result['calls_used'] == 1 and result['status'] == 'completed'
    assert len(calls) == 1
    engine.dispose()
