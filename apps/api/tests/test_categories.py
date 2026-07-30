def test_category_crud_and_child_delete_constraint(client):
    test_client, _ = client
    parent = test_client.post(
        "/api/categories",
        json={"slug": "research", "name": "研究"},
    )
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    child = test_client.post(
        "/api/categories",
        json={
            "slug": "research-ai",
            "name": "人工智能",
            "parent_id": parent_id,
        },
    )
    assert child.status_code == 201
    child_id = child.json()["id"]

    blocked = test_client.delete(f"/api/categories/{parent_id}")
    assert blocked.status_code == 409
    assert "子分类" in blocked.json()["detail"]

    renamed = test_client.patch(
        f"/api/categories/{child_id}",
        json={"name": "AI 研究"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "AI 研究"

    assert test_client.delete(f"/api/categories/{child_id}").status_code == 204
    assert test_client.delete(f"/api/categories/{parent_id}").status_code == 204


def test_taxonomy_can_hide_slugs_and_merge_tags(client):
    test_client, _ = client
    category = test_client.post(
        "/api/categories",
        json={"name": "无需填写代码"},
    )
    assert category.status_code == 201
    assert category.json()["slug"].startswith("category-")

    source = test_client.post("/api/tags", json={"name": "人工智慧"}).json()
    target = test_client.post("/api/tags", json={"name": "人工智能"}).json()
    renamed = test_client.patch(
        f"/api/tags/{source['id']}",
        json={"name": "AI"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "AI"
    document = test_client.post(
        "/api/notes",
        json={"title": "标签撤销测试", "content": "测试内容"},
    ).json()
    assert (
        test_client.patch(
            f"/api/documents/{document['id']}/tags",
            json={"tag_ids": [source["id"]]},
        ).status_code
        == 200
    )

    merged = test_client.post(
        f"/api/tags/{source['id']}/merge",
        json={"target_tag_id": target["id"]},
    )
    assert merged.status_code == 200
    remaining_ids = {item["id"] for item in test_client.get("/api/tags").json()}
    assert source["id"] not in remaining_ids
    assert target["id"] in remaining_ids

    history = test_client.get("/api/tags/merges/history").json()
    assert history[0]["source"]["name"] == "AI"
    assert history[0]["status"] == "active"
    undone = test_client.post(f"/api/tags/merges/{history[0]['id']}/undo")
    assert undone.status_code == 200
    assert undone.json()["name"] == "AI"
    document = test_client.get(f"/api/documents/{document['id']}").json()
    assert "AI" in [tag["name"] for tag in document["tags"]]


def test_ai_tag_merge_suggestions_are_validated(client, monkeypatch):
    test_client, _ = client
    first = test_client.post("/api/tags", json={"name": "AI"}).json()
    second = test_client.post("/api/tags", json={"name": "人工智能"}).json()

    class FakeProvider:
        def is_configured(self):
            return True

        def generate_json(self, **_kwargs):
            return {
                "groups": [
                    {
                        "target_tag_id": second["id"],
                        "source_tag_ids": [first["id"]],
                        "reason": "简称与全称",
                        "confidence": 0.95,
                    },
                    {
                        "target_tag_id": 9999,
                        "source_tag_ids": [first["id"]],
                        "reason": "无效标签",
                        "confidence": 1,
                    },
                ]
            }

    async def fake_provider(_db):
        return FakeProvider()

    monkeypatch.setattr(
        "apps.api.api.categories.build_provider_from_db",
        fake_provider,
    )
    response = test_client.post("/api/tags/suggestions")
    assert response.status_code == 200
    groups = response.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["target_tag_id"] == second["id"]
