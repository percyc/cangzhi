from io import BytesIO


def test_document_trash_restore_and_batch_management(client):
    test_client, _storage = client
    first = test_client.post(
        "/api/notes", json={"title": "保留测试一", "content": "第一条知识"}
    ).json()
    second = test_client.post(
        "/api/notes", json={"title": "保留测试二", "content": "第二条知识"}
    ).json()

    response = test_client.delete(f"/api/documents/{first['id']}")
    assert response.status_code == 200
    active_ids = {item["id"] for item in test_client.get("/api/documents").json()}
    trash_ids = {
        item["id"]
        for item in test_client.get("/api/documents?deleted=true").json()
    }
    assert first["id"] not in active_ids
    assert first["id"] in trash_ids

    restored = test_client.post(f"/api/documents/{first['id']}/restore")
    assert restored.status_code == 200

    trashed = test_client.post(
        "/api/documents/batch/trash",
        json={"document_ids": [first["id"], second["id"]]},
    )
    assert trashed.status_code == 200
    assert trashed.json()["affected"] == 2
    assert test_client.get("/api/documents").json() == []

    restored = test_client.post(
        "/api/documents/batch/restore",
        json={"document_ids": [first["id"], second["id"]]},
    )
    assert restored.status_code == 200
    assert restored.json()["affected"] == 2
    assert len(test_client.get("/api/documents").json()) == 2


def test_permanent_delete_requires_trash_and_cleans_only_unreferenced_blob(client):
    test_client, storage = client
    payload = b"shared source"
    first = test_client.post(
        "/api/files/upload",
        files={"file": ("first.txt", BytesIO(payload), "text/plain")},
    ).json()
    second = test_client.post(
        "/api/files/upload",
        files={"file": ("second.txt", BytesIO(payload), "text/plain")},
    ).json()
    blob_id = first["current_version"]["blob"]["id"]
    assert blob_id == second["current_version"]["blob"]["id"]
    assert len(list(storage._blob_path.iterdir())) == 1

    refused = test_client.delete(
        f"/api/documents/{first['id']}/permanent"
    )
    assert refused.status_code == 409

    assert test_client.delete(f"/api/documents/{first['id']}").status_code == 200
    trashed = test_client.get("/api/documents?deleted=true").json()[0]
    assert trashed["deleted_at"] is not None
    purged = test_client.delete(
        f"/api/documents/{first['id']}/permanent"
    )
    assert purged.status_code == 200
    assert purged.json()["deleted_blobs"] == 0
    assert test_client.get(f"/api/files/blobs/{blob_id}").status_code == 200
    assert len(list(storage._blob_path.iterdir())) == 1

    assert test_client.delete(f"/api/documents/{second['id']}").status_code == 200
    purged = test_client.delete(
        f"/api/documents/{second['id']}/permanent"
    )
    assert purged.status_code == 200
    assert purged.json()["deleted_blobs"] == 1
    assert test_client.get(f"/api/files/blobs/{blob_id}").status_code == 404
    assert list(storage._blob_path.iterdir()) == []


def test_batch_permanent_delete_and_empty_trash(client):
    test_client, _storage = client
    ids = [
        test_client.post(
            "/api/notes",
            json={"title": f"待删除 {index}", "content": f"内容 {index}"},
        ).json()["id"]
        for index in range(3)
    ]
    assert (
        test_client.post(
            "/api/documents/batch/trash",
            json={"document_ids": ids[:2]},
        ).status_code
        == 200
    )
    batch = test_client.post(
        "/api/documents/batch/permanent-delete",
        json={"document_ids": [ids[0]]},
    )
    assert batch.status_code == 200
    assert batch.json()["affected"] == 1
    emptied = test_client.delete("/api/documents/trash/empty")
    assert emptied.status_code == 200
    assert emptied.json()["affected"] == 1
    assert {item["id"] for item in test_client.get("/api/documents").json()} == {
        ids[2]
    }


def test_document_metadata_tags_and_batch_organization(client):
    test_client, _storage = client
    first = test_client.post(
        "/api/notes", json={"title": "原始标题", "content": "第一条知识"}
    ).json()
    second = test_client.post(
        "/api/notes", json={"title": "第二条", "content": "第二条知识"}
    ).json()
    category = test_client.post(
        "/api/categories",
        json={"slug": "research", "name": "研究资料"},
    ).json()
    tag = test_client.post(
        "/api/tags",
        json={"slug": "important", "name": "重要"},
    ).json()

    updated = test_client.patch(
        f"/api/documents/{first['id']}/metadata",
        json={"title": "修正标题", "summary": "用户修正后的摘要"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "修正标题"
    assert updated.json()["summary"]["summary"] == "用户修正后的摘要"
    assert updated.json()["summary"]["source"] == "user"

    organized = test_client.post(
        "/api/documents/batch/organize",
        json={
            "document_ids": [first["id"], second["id"]],
            "category_id": category["id"],
            "add_tag_ids": [tag["id"]],
        },
    )
    assert organized.status_code == 200
    for document_id in (first["id"], second["id"]):
        document = test_client.get(f"/api/documents/{document_id}").json()
        assert document["primary_category"]["id"] == category["id"]
        assert [item["id"] for item in document["tags"]] == [tag["id"]]

    cleared = test_client.patch(
        f"/api/documents/{first['id']}/tags",
        json={"tag_ids": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["tags"] == []
