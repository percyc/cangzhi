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
