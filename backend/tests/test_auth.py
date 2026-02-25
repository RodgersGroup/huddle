"""Tests for authentication endpoints — magic links, sessions, kiosk tokens."""

import pytest
from fastapi.testclient import TestClient

from app import app
from auth import (
    create_session_token,
    create_magic_link_token,
    get_current_user,
    COOKIE_NAME,
)
from db import get_db


@pytest.fixture()
def raw_client():
    """TestClient with NO auth overrides — for testing real auth flows."""
    app.dependency_overrides.clear()
    yield TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Magic link
# ---------------------------------------------------------------------------

def test_magic_link_creation(raw_client, seed_households):
    """POST /api/auth/magic-link stores a token in magic_links table."""
    resp = raw_client.post("/api/auth/magic-link", json={"email": "new@test.com"})
    assert resp.status_code == 200

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM magic_links WHERE email = 'new@test.com'"
        ).fetchone()
        assert row is not None
        assert row["used"] == 0


def test_magic_link_verify(raw_client, seed_households):
    """GET /auth/verify with a valid token sets the session cookie."""
    token = create_magic_link_token("test@test.com")

    resp = raw_client.get(f"/auth/verify?token={token}", follow_redirects=False)
    # Auth uses 303 See Other for the redirect
    assert resp.status_code == 303
    assert COOKIE_NAME in resp.cookies


def test_magic_link_verify_invalid_token(raw_client):
    """GET /auth/verify with a bad token does not set a session cookie."""
    resp = raw_client.get("/auth/verify?token=bogus-invalid-token", follow_redirects=False)
    assert COOKIE_NAME not in resp.cookies


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def test_session_validation(raw_client, seed_households):
    """GET /api/auth/me with a valid session cookie returns user info."""
    # Use a unique user id that doesn't conflict with migration seeder
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, email, display_name, created_at) "
            "VALUES (50, 'sessiontest@test.com', 'SessionTest', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO household_members "
            "(household_id, user_id, display_name, role, joined_at) "
            "VALUES (1, 50, 'SessionTest', 'manager', CURRENT_TIMESTAMP)"
        )
        conn.commit()

    token = create_session_token(user_id=50, household_id=1)
    resp = raw_client.get("/api/auth/me", cookies={COOKIE_NAME: token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["display_name"] == "SessionTest"
    assert body["household"]["id"] == 1


def test_unauthenticated_request_returns_401(raw_client):
    """GET /api/auth/me with no cookie returns 401."""
    resp = raw_client.get("/api/auth/me")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Kiosk token
# ---------------------------------------------------------------------------

def test_kiosk_token_auth(raw_client, seed_households):
    """Requests with a valid kiosk Bearer token authenticate as kiosk role."""
    kiosk_token = "test-kiosk-token-12345678"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO kiosk_tokens (household_id, token, label, created_at) "
            "VALUES (1, ?, 'test', CURRENT_TIMESTAMP)",
            (kiosk_token,),
        )
        conn.commit()

    resp = raw_client.get(
        "/api/adhoc",
        headers={"Authorization": f"Bearer {kiosk_token}"},
    )
    assert resp.status_code == 200
