import logging
from io import BytesIO

import pytest

from apps.api.core.config import settings


def test_note_create_edit_and_noop_versioning(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "", "content": "第一行\n\n我的想法"},
    )
    assert created.status_code == 201
    first = created.json()
    assert first["title"] == "第一行"
    assert first["current_version"]["version_number"] == 1

    updated = test_client.patch(
        f"/api/notes/{first['id']}",
        json={"title": "新标题", "content": "修改后的内容"},
    )
    assert updated.status_code == 200
    assert updated.json()["current_version"]["version_number"] == 2

    noop = test_client.patch(
        f"/api/notes/{first['id']}",
        json={"title": "新标题", "content": "修改后的内容"},
    )
    assert noop.status_code == 200
    assert noop.json()["current_version"]["version_number"] == 2


def test_note_rejects_blank_content(client):
    test_client, _ = client
    response = test_client.post(
        "/api/notes",
        json={"title": "空笔记", "content": "   \n"},
    )
    assert response.status_code == 422


def test_file_upload_reuses_blob_but_keeps_explicit_document_records(client):
    test_client, storage = client
    content = b"same knowledge"

    first = test_client.post(
        "/api/files/upload",
        files={"file": ("knowledge.txt", BytesIO(content), "text/plain")},
        data={"title": "第一份"},
    )
    second = test_client.post(
        "/api/files/upload",
        files={"file": ("copy.txt", BytesIO(content), "text/plain")},
        data={"title": "第二份"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    first_body = first.json()
    second_body = second.json()
    assert first_body["id"] != second_body["id"]
    assert (
        first_body["current_version"]["blob"]["id"]
        == second_body["current_version"]["blob"]["id"]
    )
    assert len(list((storage._blob_path).iterdir())) == 1

    blob = first_body["current_version"]["blob"]
    downloaded = test_client.get(f"/api/files/blobs/{blob['id']}/download")
    assert downloaded.status_code == 200
    assert downloaded.content == content

    downloaded_by_document = test_client.get(
        f"/api/documents/{first_body['id']}/original"
    )
    assert downloaded_by_document.status_code == 200
    assert downloaded_by_document.content == content
    assert "knowledge.txt" in downloaded_by_document.headers["content-disposition"]

    inline_preview = test_client.get(
        f"/api/documents/{first_body['id']}/original?inline=true"
    )
    assert inline_preview.status_code == 200
    assert inline_preview.headers["content-disposition"].startswith("inline;")

    overview = test_client.get("/api/documents/overview?limit=200")
    assert overview.status_code == 200
    overview_version = overview.json()[0]["current_version"]
    assert overview_version["processing_status"] == "created"
    assert "raw_content" not in overview_version
    assert "structured_content" not in overview_version


def test_file_upload_validates_extension_and_mime(client):
    test_client, _ = client
    bad_extension = test_client.post(
        "/api/files/upload",
        files={"file": ("malware.exe", BytesIO(b"x"), "application/octet-stream")},
    )
    mismatch = test_client.post(
        "/api/files/upload",
        files={"file": ("report.pdf", BytesIO(b"x"), "text/plain")},
    )
    octet_stream = test_client.post(
        "/api/files/upload",
        files={"file": ("note.md", BytesIO(b"# note"), "application/octet-stream")},
    )
    legacy_doc = test_client.post(
        "/api/files/upload",
        files={"file": ("legacy.doc", BytesIO(b"word"), "application/msword")},
    )
    xlsx = test_client.post(
        "/api/files/upload",
        files={
            "file": (
                "ledger.xlsx",
                BytesIO(b"excel"),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    xls = test_client.post(
        "/api/files/upload",
        files={
            "file": (
                "legacy.xls",
                BytesIO(b"excel"),
                "application/vnd.ms-excel",
            )
        },
    )

    assert bad_extension.status_code == 415
    assert mismatch.status_code == 415
    assert octet_stream.status_code == 201
    assert legacy_doc.status_code == 201
    assert xlsx.status_code == 201
    assert xls.status_code == 201


def test_file_upload_enforces_configured_limit(client, monkeypatch):
    test_client, storage = client
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)

    response = test_client.post(
        "/api/files/upload",
        files={
            "file": (
                "large.txt",
                BytesIO(b"x" * (1024 * 1024 + 1)),
                "text/plain",
            )
        },
    )

    assert response.status_code == 413
    assert list(storage._blob_path.iterdir()) == []


@pytest.mark.parametrize("extra_bytes, expected_status", [(0, 201), (1, 413)])
def test_docx_limit_counts_file_not_multipart_overhead(client, monkeypatch, extra_bytes, expected_status):
    test_client, storage = client
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)
    response = test_client.post(
        "/api/files/upload",
        data={"title": "Synthetic upload boundary"},
        files={"file": (
            "boundary.docx", BytesIO(b"x" * (1024 * 1024 + extra_bytes)),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )},
    )
    assert response.status_code == expected_status
    if expected_status == 413:
        assert list(storage._blob_path.iterdir()) == []


def test_upload_rejection_logs_unsupported_extension_without_input(client, caplog):
    test_client, _ = client
    with caplog.at_level(logging.WARNING, logger="apps.api.api.files"):
        response = test_client.post(
            "/api/files/upload",
            files={"file": ("malware.exe", BytesIO(b"x"), "application/octet-stream")},
        )

    assert response.status_code == 415
    rejections = [
        record for record in caplog.records
        if record.levelno >= logging.WARNING
        and "upload_type_rejected" in record.getMessage()
    ]
    assert len(rejections) == 1
    message = rejections[0].getMessage()
    assert "reason=unsupported_extension" in message
    assert "extension=other" in message
    assert "malware" not in message
    assert ".exe" not in message


def test_upload_rejection_logs_mime_mismatch_with_known_extension(client, caplog):
    test_client, _ = client
    with caplog.at_level(logging.WARNING, logger="apps.api.api.files"):
        response = test_client.post(
            "/api/files/upload",
            files={"file": ("report.pdf", BytesIO(b"x"), "text/plain")},
        )

    assert response.status_code == 415
    rejections = [
        record for record in caplog.records
        if record.levelno >= logging.WARNING
        and "upload_type_rejected" in record.getMessage()
    ]
    assert len(rejections) == 1
    message = rejections[0].getMessage()
    assert "reason=mime_mismatch" in message
    assert "extension=.pdf" in message
    assert "mime=text/plain" in message
    assert "report" not in message


def test_upload_rejection_redacts_hostile_header_and_filename(client, caplog):
    test_client, _ = client
    secret_mime = "application/x-secret-token-abcdef123456"
    secret_name = "quarterly-secret-plans.pdf"
    with caplog.at_level(logging.WARNING, logger="apps.api.api.files"):
        response = test_client.post(
            "/api/files/upload",
            files={
                "file": (
                    secret_name,
                    BytesIO(b"x"),
                    f"{secret_mime}; charset=utf-8",
                )
            },
        )

    assert response.status_code == 415
    rejections = [
        record for record in caplog.records
        if record.levelno >= logging.WARNING
        and "upload_type_rejected" in record.getMessage()
    ]
    assert len(rejections) == 1
    message = rejections[0].getMessage()
    assert "reason=mime_mismatch" in message
    assert "mime=other" in message
    assert "charset" not in message
    assert "secret" not in message.lower()
    assert "abcdef123456" not in message
    assert "pdftoken" not in message


def test_rejection_known_mime_parameters_are_not_logged(caplog):
    from fastapi import HTTPException
    from apps.api.api.files import validate_file_type

    with caplog.at_level(logging.WARNING, logger="apps.api.api.files"):
        with pytest.raises(HTTPException):
            validate_file_type("private.docx", "Application/MSWord; private=hidden-value")
    assert "mime=application/msword" in caplog.text
    assert "hidden-value" not in caplog.text
    assert "private" not in caplog.text


def test_upload_rejection_redacts_hostile_extension(client, caplog):
    test_client, _ = client
    with caplog.at_level(logging.WARNING, logger="apps.api.api.files"):
        response = test_client.post(
            "/api/files/upload",
            files={
                "file": (
                    "api-key-sk-live-9999.sh",
                    BytesIO(b"x"),
                    "application/octet-stream",
                )
            },
        )

    assert response.status_code == 415
    rejections = [
        record for record in caplog.records
        if record.levelno >= logging.WARNING
        and "upload_type_rejected" in record.getMessage()
    ]
    assert len(rejections) == 1
    message = rejections[0].getMessage()
    assert "reason=unsupported_extension" in message
    assert "extension=other" in message
    assert "sk-live" not in message
    assert ".sh" not in message


def test_accepted_upload_does_not_warn(client, caplog):
    test_client, _ = client
    with caplog.at_level(logging.WARNING, logger="apps.api.api.files"):
        response = test_client.post(
            "/api/files/upload",
            files={"file": ("note.txt", BytesIO(b"hello"), "text/plain")},
        )

    assert response.status_code == 201
    assert "upload_type_rejected" not in caplog.text
