"""Image parsing uses real dispatch; failures preserve source and prior text."""
from io import BytesIO
from subprocess import CompletedProcess

from PIL import Image
import pytest

from apps.api.models.blobs import Blob
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.parsers import image as image_module
from apps.api.parsers.pdf import PdfOcrOptions, PdfParser
from apps.worker.services import processor as proc
from apps.worker.tests.test_processor import engine, session  # noqa: F401


@pytest.mark.parametrize("recognized", [True, False])
def test_image_pipeline_preserves_original_and_schedules_derivation(session, monkeypatch, recognized):
    output = BytesIO()
    Image.new("RGB", (200, 100), "white").save(output, "PNG")
    source = output.getvalue()
    doc = Document(title="synthetic image", source_type=DocumentSourceType.file)
    session.add(doc)
    session.flush()
    blob = Blob(sha256="source", storage_key="synthetic", content_type="image/png",
                original_filename="scan.png", file_size=len(source))
    session.add(blob)
    session.flush()
    version = DocumentVersion(document_id=doc.id, version_number=1, content_hash="source",
        blob_id=blob.id, processing_status="created", raw_content="previous extraction")
    session.add(version)
    session.flush()
    doc.current_version_id = version.id
    job = ProcessingJob(document_id=doc.id, document_version_id=version.id,
        stage="parsing", config_version="1", idempotency_key="image-ocr-test")
    session.add(job)
    session.commit()
    monkeypatch.setattr(proc, "_load_content_for_parsing", lambda *args: (source, "image/png", "scan.png"))
    monkeypatch.setattr(proc, "_build_pdf_ocr_options", lambda *args: PdfOcrOptions())
    def runner(*args):
        tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        if recognized:
            tsv += "5\t1\t1\t1\t1\t1\t10\t10\t80\t20\t95\tRecognized image evidence\n"
        return CompletedProcess([], 0, tsv.encode(), b"")
    monkeypatch.setattr(image_module, "PdfParser", lambda options, **kwargs:
        PdfParser(options, tesseract_runner=runner, tesseract_lookup=lambda: "tesseract", **kwargs))
    assert proc.process_single_job(session, job.id) is recognized
    session.refresh(version)
    session.refresh(job)
    assert version.blob_id == blob.id and version.content_hash == "source"
    downstream = session.query(ProcessingJob).filter(ProcessingJob.id != job.id).all()
    if recognized:
        assert version.structured_content["document_type"] == "image"
        assert version.raw_content == "Recognized image evidence"
        assert {j.stage for j in downstream} >= {"chunking", "understanding"}
    else:
        assert job.status == "failed" and job.next_retry_at is None
        assert version.raw_content == "previous extraction"
        assert job.error_details["reason"] == "image_ocr_empty"
        assert not downstream
