"""Tests for chore endpoints — CRUD, completion, skip, household isolation."""

import json
import pytest
from datetime import datetime, date, timedelta
from tests.conftest import _set_auth, _make_tenant
from app import app
from auth import get_current_user
from fastapi.testclient import TestClient
from db import get_db
from routers.chores import is_chore_due, today_local


CHORE_PAYLOAD = {
    "name": "Vacuum lounge",
    "schedule_type": "daily",
    "people": ["TestUser", "MemberTwo"],
    "schedule_days": [],
}


def test_create_chore(client, seed_settings):
    """POST /api/chores creates a chore and returns its id."""
    resp = client.post("/api/chores", json=CHORE_PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body
    assert body["message"] == "Chore created"


def test_list_chores(client, seed_settings):
    """GET /api/chores returns chores for the household."""
    client.post("/api/chores", json=CHORE_PAYLOAD)
    client.post("/api/chores", json={**CHORE_PAYLOAD, "name": "Mop floor"})

    resp = client.get("/api/chores")
    assert resp.status_code == 200
    body = resp.json()
    assert "chores" in body
    assert len(body["chores"]) == 2


def test_complete_chore(client, seed_settings):
    """POST /api/chores/{id}/complete records a completion."""
    create = client.post("/api/chores", json=CHORE_PAYLOAD)
    chore_id = create.json()["id"]

    resp = client.post(f"/api/chores/{chore_id}/complete", json={
        "completed_by": "TestUser",
    })
    assert resp.status_code == 200

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM completions WHERE chore_id = ?", (chore_id,)
        ).fetchone()
        assert row is not None
        assert row["completed_by"] == "TestUser"


def test_skip_chore(client, seed_settings):
    """POST /api/chores/{id}/skip records a SKIPPED completion."""
    create = client.post("/api/chores", json=CHORE_PAYLOAD)
    chore_id = create.json()["id"]

    resp = client.post(f"/api/chores/{chore_id}/skip")
    assert resp.status_code == 200

    with get_db() as conn:
        row = conn.execute(
            "SELECT completed_by FROM completions WHERE chore_id = ?", (chore_id,)
        ).fetchone()
        assert row is not None
        assert row["completed_by"] == "SKIPPED"


def test_household_isolation(seed_households, seed_settings):
    """Chores in household 1 must not appear in household 2 queries."""
    t1 = _make_tenant(user_id=1, household_id=1, name="TestUser", role="manager")
    t2 = _make_tenant(user_id=10, household_id=2, name="OtherUser", role="manager")

    # Create chore in household 1
    _set_auth(t1)
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/chores", json=CHORE_PAYLOAD)

    # Create chore in household 2
    _set_auth(t2)
    c.post("/api/chores", json={**CHORE_PAYLOAD, "name": "Other chore"})

    # Check household 1
    _set_auth(t1)
    h1_chores = c.get("/api/chores").json()["chores"]
    h1_names = {ch["name"] for ch in h1_chores}
    assert "Vacuum lounge" in h1_names
    assert "Other chore" not in h1_names

    # Check household 2
    _set_auth(t2)
    h2_chores = c.get("/api/chores").json()["chores"]
    h2_names = {ch["name"] for ch in h2_chores}
    assert "Other chore" in h2_names
    assert "Vacuum lounge" not in h2_names


def test_create_chore_requires_name(client, seed_settings):
    """POST /api/chores with empty name returns 400."""
    resp = client.post("/api/chores", json={
        "name": "",
        "schedule_type": "daily",
        "people": ["TestUser"],
    })
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# is_chore_due() — schedule_type='days' overdue logic
# ---------------------------------------------------------------------------

def _make_chore_dict(schedule_days, start_date=None, created_at=None):
    """Build a minimal chore dict for is_chore_due() tests."""
    today = today_local(1)
    return {
        "id": 999,
        "schedule_type": "days",
        "schedule_days": json.dumps(schedule_days),
        "schedule_interval": None,
        "start_date": start_date or (today - timedelta(days=1)).isoformat(),
        "created_at": created_at or datetime.now().isoformat(),
        "people": '["TestUser"]',
    }


def test_days_chore_created_today_not_overdue(seed_settings):
    """A 'days' chore created today should NOT be overdue for earlier days this week."""
    today = today_local(1)
    # Schedule for every day of the week so we know today is a scheduled day
    all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    chore = _make_chore_dict(
        schedule_days=all_days,
        start_date=(today - timedelta(days=7)).isoformat(),  # start_date a week ago
        created_at=datetime.now().isoformat(),  # but created just now
    )

    with get_db() as conn:
        result = is_chore_due(chore, conn, household_id=1)

    # Should be due_today (it's a scheduled day), NOT overdue
    assert result["status"] in ("due_today",), (
        f"Expected 'due_today' but got '{result['status']}' with days_overdue={result['days_overdue']}"
    )


def test_days_chore_created_midweek_not_overdue_for_earlier_days(seed_settings):
    """A chore created on Wednesday shouldn't be overdue for Monday."""
    today = today_local(1)
    # Create a chore scheduled Mon/Wed/Fri, created today
    chore = _make_chore_dict(
        schedule_days=["Monday", "Wednesday", "Friday"],
        start_date=(today - timedelta(days=14)).isoformat(),  # start_date 2 weeks ago
        created_at=datetime.now().isoformat(),  # created right now
    )

    with get_db() as conn:
        result = is_chore_due(chore, conn, household_id=1)

    # It should NOT be overdue — any past scheduled days are before created_at
    assert result["status"] != "overdue" or result["days_overdue"] == 0, (
        f"New chore should not be overdue, got status='{result['status']}' days_overdue={result['days_overdue']}"
    )


def test_days_chore_genuinely_overdue(seed_settings):
    """A chore created days ago with no completions on a past scheduled day IS overdue."""
    today = today_local(1)
    # Created 10 days ago, scheduled every day of the week
    all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    created = (datetime.now() - timedelta(days=10)).isoformat()
    chore = _make_chore_dict(
        schedule_days=all_days,
        start_date=(today - timedelta(days=10)).isoformat(),
        created_at=created,
    )

    with get_db() as conn:
        result = is_chore_due(chore, conn, household_id=1)

    # Yesterday was a scheduled day with no completion → should be overdue
    assert result["status"] == "overdue"
    assert result["days_overdue"] >= 1


def test_days_chore_completed_on_last_scheduled_day(seed_settings):
    """A chore completed on its last scheduled day is not overdue."""
    today = today_local(1)
    all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    created = (datetime.now() - timedelta(days=10)).isoformat()
    chore_dict = _make_chore_dict(
        schedule_days=all_days,
        start_date=(today - timedelta(days=10)).isoformat(),
        created_at=created,
    )

    with get_db() as conn:
        # Insert a real chore row so completions can reference it
        conn.execute(
            "INSERT INTO chores (id, name, schedule_type, schedule_days, people, current_person_index, start_date, created_at, household_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (999, "Test chore", "days", json.dumps(all_days), '["TestUser"]', 0,
             chore_dict["start_date"], chore_dict["created_at"], 1),
        )
        # Insert completion for today
        conn.execute(
            "INSERT INTO completions (chore_id, completed_by, completed_at, household_id) VALUES (?, ?, ?, ?)",
            (999, "TestUser", datetime.now().isoformat(), 1),
        )
        conn.commit()

        result = is_chore_due(chore_dict, conn, household_id=1)

    assert result["status"] == "completed_today"


def test_days_chore_future_start_date_not_due(seed_settings):
    """A chore with start_date in the future should be not_due."""
    today = today_local(1)
    future = (today + timedelta(days=5)).isoformat()
    chore = _make_chore_dict(
        schedule_days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        start_date=future,
        created_at=datetime.now().isoformat(),
    )

    with get_db() as conn:
        result = is_chore_due(chore, conn, household_id=1)

    assert result["status"] in ("not_due", "tomorrow")
