from apps.api.services.webdav import parse_multistatus, validate_webdav_url


def test_parse_webdav_multistatus():
    payload = b"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/docs/</d:href><d:propstat><d:prop>
        <d:resourcetype><d:collection/></d:resourcetype>
      </d:prop></d:propstat></d:response>
      <d:response><d:href>/docs/a.md</d:href><d:propstat><d:prop>
        <d:resourcetype/><d:getetag>"abc"</d:getetag>
        <d:getcontentlength>42</d:getcontentlength>
        <d:getcontenttype>text/markdown</d:getcontenttype>
      </d:prop></d:propstat></d:response>
    </d:multistatus>"""
    entries = parse_multistatus(payload)
    assert entries[0].is_collection is True
    assert entries[1].path == "/docs/a.md"
    assert entries[1].etag == '"abc"'
    assert entries[1].size == 42


def test_trusted_private_webdav_requires_http_url():
    assert (
        validate_webdav_url(
            "http://192.168.1.2:5005/dav",
            trusted_private_network=True,
        )
        == "http://192.168.1.2:5005/dav/"
    )


def test_created_webdav_source_is_returned_in_connector_list(client):
    test_client, _storage = client
    created = test_client.post(
        "/api/webdav",
        json={
            "name": "个人云盘",
            "base_url": "https://dav.example.com/",
            "username": "reader",
            "password": "secret",
            "root_path": "/knowledge",
        },
    )
    assert created.status_code == 201

    response = test_client.get("/api/webdav")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["name"] == "个人云盘"
    assert body[0]["entry_counts"] == {
        "total": 0,
        "pending": 0,
        "failed": 0,
        "synced": 0,
    }
