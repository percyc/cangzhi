from io import BytesIO

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
