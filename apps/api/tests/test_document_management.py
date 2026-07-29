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
