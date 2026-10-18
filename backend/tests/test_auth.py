import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.auth import (
    COOKIE,
    SESSION_SECONDS,
    install_auth,
    issue_session,
    password_digest,
    valid_session,
)
from backend.app.config import Settings

ORIGIN = "https://workspace.example.test"
KEY = "test-session-key-that-is-at-least-32-characters"
SALT = "00112233445566778899aabbccddeeff"
PASSWORD = "test-only-workspace-password"


def client(*, enabled=True, public=False):
    settings = Settings(
        _env_file=None,
        hosted_auth_enabled=enabled,
        public_browsing_enabled=public,
        hosted_session_secret=KEY,
        hosted_password_hash=f"{SALT}:{password_digest(PASSWORD, SALT)}",
        public_app_origin=ORIGIN,
    )
    app = FastAPI()
    install_auth(app, settings)

    @app.get("/api/private")
    def private():
        return {"protected": True}

    @app.post("/api/private")
    def mutate():
        return {"changed": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    for path in [
        "/api/snapshots",
        "/api/snapshots/saved/analysis",
        "/api/snapshots/saved/source",
        "/api/snapshots/saved/graph/callers",
        "/api/investigations",
        "/api/investigations/saved",
        "/api/diff/jobs/saved",
    ]:
        app.add_api_route(path, private, methods=["GET"])
    for path in [
        "/api/query/investigate",
        "/api/diff/jobs",
        "/api/snapshots/ingest",
        "/api/snapshots/saved/pin",
        "/api/runtime/action/saved/execute",
        "/api/repos/register",
        "/api/investigations/saved/notes",
        "/api/evidence/saved/replay",
    ]:
        app.add_api_route(path, mutate, methods=["POST"])
    return TestClient(app, base_url=ORIGIN)


def test_hosted_mode_fails_closed_without_required_secrets():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, hosted_auth_enabled=True)


def test_session_expiry_tampering_and_rotation():
    token = issue_session(KEY, now=100)
    assert valid_session(token, KEY, now=101)
    assert not valid_session(token, KEY, now=100 + SESSION_SECONDS)
    assert not valid_session(token + "x", KEY, now=101)
    assert not valid_session(token, KEY + "rotated", now=101)
    assert not valid_session("invalid", KEY)
    assert not valid_session("a" * 1000, KEY)


def test_protected_routes_and_login_cookie():
    c = client()
    blocked = c.get("/api/private")
    assert blocked.status_code == 401
    assert blocked.headers["cache-control"] == "private, no-store"
    assert c.get("/docs").status_code == 401
    assert c.get("/health").status_code == 200
    assert c.get("/api/auth/session").json() == {
        "enabled": True,
        "authenticated": False,
        "public_browsing": False,
    }
    response = c.post("/api/auth/session", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert all(
        flag in cookie for flag in [COOKIE, "HttpOnly", "Secure", "SameSite=strict", "Path=/"]
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert c.get("/api/private").status_code == 200
    assert c.get("/api/auth/session").json()["authenticated"]
    assert c.post("/api/private", headers={"Origin": ORIGIN}).status_code == 200
    assert c.delete("/api/auth/session", headers={"Origin": ORIGIN}).status_code == 200
    assert c.get("/api/private").status_code == 401


def test_csrf_wrong_password_and_throttling():
    c = client()
    for origin in [None, "https://attacker.example.test"]:
        headers = {"Origin": origin} if origin else {}
        assert (
            c.post("/api/auth/session", json={"password": PASSWORD}, headers=headers).status_code
            == 403
        )
    for _ in range(10):
        assert (
            c.post(
                "/api/auth/session", json={"password": "wrong"}, headers={"Origin": ORIGIN}
            ).status_code
            == 401
        )
    response = c.post("/api/auth/session", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "300"


def test_local_development_does_not_require_login():
    c = client(enabled=False)
    assert c.get("/api/private").status_code == 200
    assert c.get("/api/auth/session").json() == {
        "enabled": False,
        "authenticated": True,
        "public_browsing": False,
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/snapshots",
        "/api/snapshots/saved/analysis",
        "/api/snapshots/saved/source",
        "/api/snapshots/saved/graph/callers",
        "/api/investigations",
        "/api/investigations/saved",
        "/api/diff/jobs/saved",
    ],
)
def test_public_visitors_can_reopen_and_explore_saved_work(path):
    c = client(public=True)
    assert c.get(path).status_code == 200
    assert c.get("/api/auth/session").json() == {
        "enabled": True,
        "authenticated": False,
        "public_browsing": True,
    }


@pytest.mark.parametrize("path", ["/api/query/investigate", "/api/diff/jobs"])
def test_public_interactive_exploration_retains_origin_check(path):
    c = client(public=True)
    assert c.post(path, headers={"Origin": ORIGIN}).status_code == 200
    assert c.post(path, headers={"Origin": "https://other.test"}).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/snapshots/ingest",
        "/api/snapshots/saved/pin",
        "/api/runtime/action/saved/execute",
        "/api/repos/register",
        "/api/investigations/saved/notes",
        "/api/evidence/saved/replay",
        "/api/private",
    ],
)
def test_public_mode_keeps_owner_operations_protected(path):
    c = client(public=True)
    assert c.post(path, headers={"Origin": ORIGIN}).status_code == 401
    assert c.get("/docs").status_code == 401
    assert c.get("/api/private").status_code == 401
    c.post("/api/auth/session", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert c.post(path, headers={"Origin": ORIGIN}).status_code == 200
    c.delete("/api/auth/session", headers={"Origin": ORIGIN})
    assert c.get("/api/snapshots").status_code == 200
    assert c.post(path, headers={"Origin": ORIGIN}).status_code == 401
