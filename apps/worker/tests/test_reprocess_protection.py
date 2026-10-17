from types import SimpleNamespace

from apps.api.models.documents import Document, DocumentVersion, DocumentSourceType
from apps.api.models.processing import ProcessingJob
from apps.api.models.taxonomy import Category, DocumentCategory, DocumentSummary, DocumentTag, Tag
from apps.api.ai.schema import UnderstandingResult
from apps.api.parsers.base import ParserResult, StructuredContent, Block
from apps.worker.services import processor as proc
from apps.worker.tests.test_processor import engine, session  # noqa: F401


def seed(session):
    doc = Document(title="manual title", source_type=DocumentSourceType.file)
    session.add(doc)
    session.flush()
    version = DocumentVersion(document_id=doc.id,version_number=1,content_hash="source",
                              raw_content="previous good content", processing_status="ready")
    session.add(version)
    session.flush()
    doc.current_version_id=version.id
    session.commit()
    return doc, version


def test_low_quality_parse_preserves_previous_text(session, monkeypatch):
    doc,version=seed(session)
    job=ProcessingJob(document_id=doc.id,document_version_id=version.id,stage="parsing",config_version="2",idempotency_key="quality-test")
    session.add(job)
    session.commit()
    monkeypatch.setattr(proc,"_load_content_for_parsing",lambda *args:(b"pdf","application/pdf","test.pdf"))
    result=ParserResult(True,StructuredContent("pdf",[Block("paragraph","garbled",[])],
                       metadata={"pdf_extraction":{"low_quality_pages":[12]}}))
    monkeypatch.setattr(proc,"get_parser_for_content",lambda *args,**kwargs:SimpleNamespace(parse=lambda *args:result))
    assert not proc.process_single_job(session,job.id)
    session.refresh(version)
    session.refresh(job)
    assert version.raw_content=="previous good content"
    assert job.status=="failed" and job.next_retry_at is None
    assert job.error_details["reason"]=="ocr_quality_review"
    assert session.query(ProcessingJob).count()==1


def test_reprocess_keeps_user_organization_and_refreshes_model_fields(session):
    doc,version=seed(session)
    category=Category(name="manual",slug="manual")
    tag=Tag(name="manual",slug="manual")
    session.add_all([category,tag])
    session.flush()
    session.add_all([
        DocumentCategory(document_id=doc.id,document_version_id=version.id,category_id=category.id,is_primary=True,source="user"),
        DocumentSummary(document_id=doc.id,document_version_id=version.id,summary="manual summary",source="user"),
        DocumentTag(document_id=doc.id,document_version_id=version.id,tag_id=tag.id,source="user"),
    ])
    session.commit()
    proc._clear_understanding_results(session,version.id)
    proc._persist_understanding_result(session,version,doc,
        UnderstandingResult(summary="new AI summary",category_slug="manual",tags=["manual"],confidence=0.9),[category])
    session.commit()
    assert session.query(DocumentSummary).one().summary=="manual summary"
    assert session.query(DocumentCategory).one().source=="user"
    assert session.query(DocumentTag).one().source=="user"
    assert doc.title=="manual title"


def test_explicit_empty_manual_fields_remain_empty(session):
    doc,version=seed(session)
    version.meta={"manual_summary":True,"manual_tags":True}
    session.commit()
    proc._add_understanding_unless_user_owned(session,DocumentSummary(document_id=doc.id,document_version_id=version.id,summary="AI",source="model"))
    session.flush()
    assert session.query(DocumentSummary).count()==0


def test_generated_summary_replaced_only_after_new_result(session):
    doc,version=seed(session)
    category=Category(name="general",slug="general")
    session.add(category)
    session.add(DocumentSummary(document_id=doc.id,document_version_id=version.id,summary="old AI",source="model"))
    session.commit()
    proc._clear_understanding_results(session,version.id)
    session.commit()
    assert session.query(DocumentSummary).one().summary=="old AI"
    proc._persist_understanding_result(session,version,doc,
        UnderstandingResult(summary="new AI",category_slug="general",tags=[],confidence=0.9),[category])
    session.commit()
    assert session.query(DocumentSummary).one().summary=="new AI"


def test_reprocessing_preserves_presentation_but_first_parse_can_extract_title(session):
    doc,version=seed(session)
    structured=StructuredContent("txt",[Block("paragraph","new body",[])],metadata={"title":"parser title"})
    proc._apply_parse_result(session,doc,version,structured,preserve_presentation=True)
    assert doc.title=="manual title"
    proc._apply_parse_result(session,doc,version,structured)
    assert doc.title=="parser title"
