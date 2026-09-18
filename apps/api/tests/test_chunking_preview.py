import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import apps.api.models  # noqa: F401
from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.workspaces import Workspace
from apps.api.services.workspaces import bind_workspace_context
from apps.api.services import chunking_preview as service
from apps.api.tests.test_chunking_candidate import payload


def test_preview_is_cached_scoped_and_non_destructive(monkeypatch):
    calls = []
    fake = SimpleNamespace(name="test", _model="model", _base_url="local", prompt_version="v1")
    def generate(**_):
        calls.append(1)
        return {"ends": [5]}
    fake.generate_json = generate
    async def configured(_db):
        return fake
    monkeypatch.setattr(service, "build_provider_from_db", configured)

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add_all([Workspace(id=1, slug="one", name="one"), Workspace(id=2, slug="two", name="two")])
            doc = Document(workspace_id=1, title="sample", source_type=DocumentSourceType.note)
            db.add(doc)
            await db.flush()
            original = payload()
            version = DocumentVersion(document_id=doc.id, version_number=1, content_hash="original", structured_content=original, raw_content="original text", processing_status="ready")
            db.add(version)
            await db.flush()
            doc.current_version_id = version.id
            await db.commit()
            doc_id, version_id = doc.id, version.id
            bind_workspace_context(db.sync_session, 1)
            result = await service.preview_document(db, doc_id)
            assert result["accepted_windows"] == 1 and result["activated"] is False
            assert "specs" not in result and len(result["samples"]) <= 5
            assert (await service.preview_document(db, doc_id))["cached"] is True
            assert len(calls) == 1
            stored = await db.get(DocumentVersion, version_id, populate_existing=True)
            assert stored.structured_content == original
            assert stored.raw_content == "original text" and stored.content_hash == "original"
            fake._model = "new model"
            await service.preview_document(db, doc_id)
            assert len(calls) == 2
            await service.preview_document(db, doc_id, refresh=True)
            assert len(calls) == 3
            stored = await db.get(DocumentVersion, version_id)
            changed = payload()
            changed["blocks"][0]["text"] = "updated original"
            stored.structured_content = changed
            await db.commit()
            await service.preview_document(db, doc_id)
            assert len(calls) == 4  # source fingerprint invalidates the cache

            async def change_source_during_generation(fn, source, **kwargs):
                result = fn(source, **kwargs)
                current = await db.get(DocumentVersion, version_id)
                current.structured_content = payload(7)
                await db.commit()
                return result

            monkeypatch.setattr(service.asyncio, "to_thread", change_source_during_generation)
            with pytest.raises(service.PreviewError) as error:
                await service.preview_document(db, doc_id, refresh=True)
            assert error.value.status == 409
            assert len(calls) == 5
            bind_workspace_context(db.sync_session, 2)
            with pytest.raises(service.PreviewError) as error:
                await service.preview_document(db, doc_id)
            assert error.value.status == 404
            assert len(calls) == 5
        await engine.dispose()
    asyncio.run(run())


def test_endpoint_requires_admin(client):
    from apps.api.api.auth import require_admin
    http, _ = client
    http.app.dependency_overrides.pop(require_admin, None)
    assert http.post("/api/documents/1/chunking-preview").status_code == 401


@pytest.mark.parametrize("kind", ["pdf", "doc", "docx", "markdown", "html", "note", "xlsx"])
def test_preview_selects_policy_without_changing_deployed_pdf(kind):
    from apps.api.services import chunking_candidate, chunking_adaptive
    policy, builder = service.candidate_policy({"document_type": kind})
    if kind == "pdf":
        assert policy == chunking_candidate.POLICY_VERSION
        assert builder is chunking_candidate.build_candidate
    else:
        assert policy == chunking_adaptive.POLICY_VERSION
        assert builder is chunking_adaptive.build_adaptive_candidate


def test_pdf_preview_retains_false_heading_repair_without_source_changes():
    import copy
    source = payload(kind="pdf")
    for block in source["blocks"]:
        block.update(type="heading", level=1, heading_path=[block["text"]])
    before = copy.deepcopy(source)
    _, builder = service.candidate_policy(source)
    result = builder(source, provider=None)
    assert result["candidate"]["children"] == 1
    child = next(spec for spec in result["specs"] if spec["role"] == "child")
    assert child["content"] == "\n\n".join(block["text"] for block in source["blocks"])
    assert child["heading_path"] == []
    assert source == before
