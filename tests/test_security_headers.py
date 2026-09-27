"""Security response headers, and the request-size backstop.

Neither had a test before this: `app/main.py`'s `add_security_headers` and
`reject_oversized_requests` were only ever verified by hand. Both are cheap to
get wrong silently -- a header that is only ever `setdefault`'d can vanish
under a refactor with no visible symptom until a reviewer runs `curl -I`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_a_json_route_gets_the_full_set_of_security_headers(client: TestClient):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in r.headers["strict-transport-security"]
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_the_interactive_docs_get_headers_but_not_a_blocking_csp(client: TestClient):
    """Swagger UI loads its own bundle from a CDN; a strict CSP would break it,
    but there is no reason to skip the headers that do not interfere with it."""
    r = client.get("/docs")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in r.headers


def test_the_embed_frame_keeps_its_own_headers_untouched(client: TestClient, make_event):
    """The embed page's entire feature is being framed by someone else's site --
    the global `X-Frame-Options: DENY` must never reach it."""
    event = make_event()
    r = client.get(f"/embed/gallery/{event.slug}")
    assert r.status_code == 200
    assert "x-frame-options" not in r.headers
    # embed.py's own, deliberately permissive, policy is still exactly what it sets.
    assert "frame-ancestors *" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"


def test_a_route_specific_header_is_never_overwritten(client: TestClient, make_event):
    """`setdefault`, not an overwrite: any route that has already set a header
    this middleware would also set must win."""
    event = make_event()
    r = client.get(f"/embed/gallery/{event.slug}")
    assert r.headers["referrer-policy"] == "no-referrer"  # not the global default


def test_a_request_over_the_size_cap_is_rejected_before_the_body_is_read(client: TestClient):
    from app.main import MAX_REQUEST_BYTES

    r = client.post(
        "/api/auth/login",
        headers={"content-length": str(MAX_REQUEST_BYTES + 1)},
        content=b"{}",
    )
    assert r.status_code == 413
    assert "MB" in r.json()["detail"]


def test_a_request_under_the_size_cap_is_not_affected(client: TestClient):
    r = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "wrong"})
    assert r.status_code in (401, 422)  # rejected for being wrong, not for size
