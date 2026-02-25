"""Tests for health check endpoints."""


def test_health_ping_returns_ok(client):
    """GET /api/health/ping returns simple ok status."""
    resp = client.get("/api/health/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_check_returns_healthy(client, seed_settings):
    """GET /api/health returns healthy when DB is reachable."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"


def test_health_check_has_expected_keys(client, seed_settings):
    """GET /api/health response contains status, timestamp, and checks."""
    resp = client.get("/api/health")
    body = resp.json()
    assert "status" in body
    assert "timestamp" in body
    assert "checks" in body
    assert "database" in body["checks"]
    assert body["checks"]["database"]["status"] == "ok"
