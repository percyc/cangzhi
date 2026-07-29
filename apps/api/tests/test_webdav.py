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
