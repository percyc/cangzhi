from io import BytesIO
import json
from urllib.parse import unquote
from zipfile import ZipFile


def test_single_document_markdown_and_json_exports(client):
    test_client, _storage = client
    document = test_client.post(
        "/api/notes",
        json={
            "title": "项目/复盘",
            "content": "# 结论\n\n继续推进。",
        },
    ).json()

    markdown = test_client.get(
        f"/api/exports/documents/{document['id']}/markdown"
    )
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "项目-复盘" in unquote(markdown.headers["content-disposition"])
    assert "# 项目/复盘" in markdown.text
    assert "继续推进。" in markdown.text

    exported_json = test_client.get(
        f"/api/exports/documents/{document['id']}/json"
    )
    assert exported_json.status_code == 200
    payload = exported_json.json()
    assert payload["schema_version"] == "cangzhi.document.v1"
    assert payload["title"] == "项目/复盘"
    assert payload["current_version"]["raw_content"] == "# 结论\n\n继续推进。"


def test_library_export_is_portable_and_honours_options(client):
    test_client, _storage = client
    note = test_client.post(
        "/api/notes",
        json={"title": "随手记", "content": "一条想法"},
    ).json()
    uploaded = test_client.post(
        "/api/files/upload",
        files={"file": ("source.txt", BytesIO(b"source body"), "text/plain")},
        data={"title": "带原文资料"},
    ).json()
    test_client.delete(f"/api/documents/{note['id']}")

    active_only = test_client.get("/api/exports/library")
    assert active_only.status_code == 200
    with ZipFile(BytesIO(active_only.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["document_count"] == 1
        assert manifest["documents"][0]["id"] == uploaded["id"]
        assert manifest["documents"][0]["original"] is None
        assert any(name.startswith("knowledge/") for name in archive.namelist())
        assert any(name.startswith("data/") for name in archive.namelist())
        assert not any(name.startswith("originals/") for name in archive.namelist())

    complete = test_client.get(
        "/api/exports/library?include_trashed=true&include_originals=true"
    )
    assert complete.status_code == 200
    with ZipFile(BytesIO(complete.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["document_count"] == 2
        assert manifest["include_trashed"] is True
        assert manifest["include_originals"] is True
        original_path = next(
            item["original"]
            for item in manifest["documents"]
            if item["id"] == uploaded["id"]
        )
        assert archive.read(original_path) == b"source body"
