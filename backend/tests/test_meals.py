"""Tests for meal duty rotation configuration."""

import json
from tests.conftest import _set_auth, _make_tenant
from fastapi.testclient import TestClient
from app import app
from db import get_db


def _seed_household_with_members(household_id=1):
    """Create a household with 3 members for rotation testing."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO households (id, name, slug, tier, timezone, created_at) "
            "VALUES (?, 'Test House', 'test-house', 'free', 'Australia/Sydney', CURRENT_TIMESTAMP)",
            (household_id,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, email, display_name, created_at) "
            "VALUES (1, 'test@test.com', 'Keiran', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, email, display_name, created_at) "
            "VALUES (2, 'test2@test.com', 'Ciara', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, email, display_name, created_at) "
            "VALUES (3, 'test3@test.com', 'Tahni', CURRENT_TIMESTAMP)"
        )
        for uid, name in [(1, "Keiran"), (2, "Ciara"), (3, "Tahni")]:
            conn.execute(
                "INSERT OR IGNORE INTO household_members (household_id, user_id, display_name, role, joined_at) "
                "VALUES (?, ?, ?, 'member', CURRENT_TIMESTAMP)",
                (household_id, uid, name),
            )
        conn.commit()


def _get_client(household_id=1):
    tenant = _make_tenant(user_id=1, household_id=household_id, name="Keiran", role="manager")
    _set_auth(tenant)
    return TestClient(app, raise_server_exceptions=False)


def test_default_rotation_order_uses_member_id():
    """Without custom order, rotation uses household_members.id order."""
    _seed_household_with_members()
    client = _get_client()
    resp = client.get("/api/meals/duties")
    assert resp.status_code == 200
    data = resp.json()["duties"]
    assert data["rotation_order"] == ["Keiran", "Ciara", "Tahni"]


def test_custom_rotation_order():
    """Custom rotation order setting changes the assignment."""
    _seed_household_with_members()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
            ("meal_rotation_order", json.dumps(["Tahni", "Keiran", "Ciara"]), 1),
        )
        conn.commit()

    client = _get_client()
    resp = client.get("/api/meals/duties")
    assert resp.status_code == 200
    data = resp.json()["duties"]
    assert data["rotation_order"] == ["Tahni", "Keiran", "Ciara"]


def test_custom_order_removes_departed_members():
    """If a member in the custom order no longer exists, they are silently dropped."""
    _seed_household_with_members()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
            ("meal_rotation_order", json.dumps(["Tahni", "Ghost", "Keiran", "Ciara"]), 1),
        )
        conn.commit()

    client = _get_client()
    resp = client.get("/api/meals/duties")
    data = resp.json()["duties"]
    assert data["rotation_order"] == ["Tahni", "Keiran", "Ciara"]


def test_excluded_days_in_response():
    """Excluded days appear in the duties response and have no cook assigned."""
    _seed_household_with_members()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
            ("meal_excluded_days", json.dumps({"4": "Takeaway", "5": "Leftovers"}), 1),
        )
        conn.commit()

    client = _get_client()
    resp = client.get("/api/meals/duties")
    data = resp.json()["duties"]
    assert data["excluded_days"] == {"4": "Takeaway", "5": "Leftovers"}
    # Day 4 (Friday) and 5 (Saturday) should not have cook assignments
    assert "4" not in data["cook_lunch"] and 4 not in data["cook_lunch"]
    assert "5" not in data["cook_dinner"] and 5 not in data["cook_dinner"]


def test_no_excluded_days_by_default():
    """Without the setting, excluded_days is an empty dict."""
    _seed_household_with_members()
    client = _get_client()
    resp = client.get("/api/meals/duties")
    data = resp.json()["duties"]
    assert data["excluded_days"] == {}
    # All 7 days should have cook assignments
    assert len(data["cook_lunch"]) == 7


def test_swap_permanent_changes_rotation():
    """A permanent swap changes the person_index without setting override flag."""
    _seed_household_with_members()
    client = _get_client()
    # Trigger initial duties
    client.get("/api/meals/duties")

    resp = client.put("/api/meals/duties/swap", json={
        "duty_type": "cook_dinner",
        "day_of_week": 0,
        "person": "Tahni",
        "permanent": True,
    })
    assert resp.status_code == 200

    with get_db() as conn:
        row = conn.execute(
            "SELECT is_override FROM meal_duties WHERE household_id = 1 AND duty_type = 'cook_dinner' AND day_of_week = 0"
        ).fetchone()
        assert row["is_override"] == 0


def test_swap_oneoff_sets_override_flag():
    """A one-off swap sets is_override = 1 and stores original index."""
    _seed_household_with_members()
    client = _get_client()
    client.get("/api/meals/duties")

    resp = client.put("/api/meals/duties/swap", json={
        "duty_type": "cook_dinner",
        "day_of_week": 0,
        "person": "Tahni",
        "permanent": False,
    })
    assert resp.status_code == 200

    with get_db() as conn:
        row = conn.execute(
            "SELECT is_override, override_original_index FROM meal_duties "
            "WHERE household_id = 1 AND duty_type = 'cook_dinner' AND day_of_week = 0"
        ).fetchone()
        assert row["is_override"] == 1
        assert row["override_original_index"] is not None


def test_swap_default_is_oneoff():
    """If permanent is not specified, swap defaults to one-off."""
    _seed_household_with_members()
    client = _get_client()
    client.get("/api/meals/duties")

    resp = client.put("/api/meals/duties/swap", json={
        "duty_type": "cook_lunch",
        "day_of_week": 1,
        "person": "Ciara",
    })
    assert resp.status_code == 200

    with get_db() as conn:
        row = conn.execute(
            "SELECT is_override FROM meal_duties WHERE household_id = 1 AND duty_type = 'cook_lunch' AND day_of_week = 1"
        ).fetchone()
        assert row["is_override"] == 1


def test_get_duties_config_defaults():
    """Config endpoint returns empty defaults when no settings configured."""
    _seed_household_with_members()
    client = _get_client()
    resp = client.get("/api/meals/duties/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["rotation_order"] == ["Keiran", "Ciara", "Tahni"]
    assert data["excluded_days"] == {}


def test_put_duties_config_rotation_order():
    """Setting a custom rotation order via config endpoint."""
    _seed_household_with_members()
    client = _get_client()
    resp = client.put("/api/meals/duties/config", json={
        "rotation_order": ["Tahni", "Keiran", "Ciara"],
    })
    assert resp.status_code == 200

    resp = client.get("/api/meals/duties/config")
    assert resp.json()["rotation_order"] == ["Tahni", "Keiran", "Ciara"]


def test_put_duties_config_excluded_days():
    """Setting excluded days via config endpoint."""
    _seed_household_with_members()
    client = _get_client()
    resp = client.put("/api/meals/duties/config", json={
        "excluded_days": {"4": "Takeaway", "5": "Leftovers"},
    })
    assert resp.status_code == 200

    resp = client.get("/api/meals/duties/config")
    assert resp.json()["excluded_days"] == {"4": "Takeaway", "5": "Leftovers"}


def test_put_duties_config_validates_members():
    """Config endpoint rejects rotation order with unknown members."""
    _seed_household_with_members()
    client = _get_client()
    resp = client.put("/api/meals/duties/config", json={
        "rotation_order": ["Keiran", "Ghost", "Ciara"],
    })
    assert resp.status_code == 400
    assert "Ghost" in resp.json()["detail"]


def test_put_duties_config_validates_day_range():
    """Config endpoint rejects excluded days outside 0-6 range."""
    _seed_household_with_members()
    client = _get_client()
    resp = client.put("/api/meals/duties/config", json={
        "excluded_days": {"7": "Invalid"},
    })
    assert resp.status_code == 400


def test_excluded_days_removes_duty_rows():
    """Excluding a day removes its duty rows from the DB."""
    _seed_household_with_members()
    client = _get_client()
    client.get("/api/meals/duties")

    with get_db() as conn:
        count_before = conn.execute(
            "SELECT COUNT(*) as c FROM meal_duties WHERE household_id = 1 AND day_of_week = 4"
        ).fetchone()["c"]
        assert count_before > 0

    client.put("/api/meals/duties/config", json={"excluded_days": {"4": "Takeaway"}})

    with get_db() as conn:
        count_after = conn.execute(
            "SELECT COUNT(*) as c FROM meal_duties WHERE household_id = 1 AND day_of_week = 4"
        ).fetchone()["c"]
        assert count_after == 0
