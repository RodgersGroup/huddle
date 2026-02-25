"""
Test fixtures for Huddle.

Uses a temporary SQLite database file for each test session so the real
chores.db is never touched. The database is initialised with the full
migration suite, then test data is seeded (two households to verify
multi-tenant isolation).

Auth is handled by overriding FastAPI's get_current_user dependency to
return a TenantContext directly, avoiding the need for real cookies or
magic links in most tests.

TEST COVERAGE PRIORITIES (modules that need tests, ordered by risk):

  1. auth.py — Magic link flow, session management, household create/join,
     role enforcement, rate limiting. Highest risk: auth bypass, tenant
     isolation failure, rate limiter edge cases.

  2. background.py — Push notification scheduling, watchdog, screen control.
     Hard to test (async loops, time-dependent), but bugs here cause missed
     notifications or stuck kiosks.

  3. routers/chores.py — Completion, rotation, streaks, load balancing,
     overdue logic. Core feature; complex state machine with many edge
     cases around scheduling and person rotation.

  4. routers/calendar.py — Recurring event expansion, exceptions, multi-day
     events. Recurrence logic is notoriously tricky.

  5. routers/bills.py — Recurring bill generation on payment. Financial
     data; incorrect auto-generation creates user-visible problems.

  6. routers/meals.py — Meal duty rotation, swap/override logic.
     Rotation state machine with permanent vs one-off overrides.

  7. push.py — Subscription management, expired subscription cleanup.
     Failure modes are silent (user just stops getting notifications).

  8. routers/recipes.py — URL import (JSON-LD parsing), ingredient scaling,
     to-shopping dedup. External data parsing is inherently fragile.

  9. db.py — Thread-local connection caching, rollback behaviour.
     Foundational; bugs here affect everything.

 10. migrations/runner.py — Migration ordering, rename tracking, rollback
     on failure. Schema corruption risk if migrations misbehave.

Existing test files: test_shopping, test_adhoc, test_auth, test_chores,
test_feedback, test_health. These provide baseline coverage for CRUD
operations and tenant isolation but do not cover edge cases listed above.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

# Point db.DB_PATH at a temporary file BEFORE importing anything that
# touches the database.  This must happen before `from app import app`.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db_path = Path(_tmp_db.name)
_tmp_db.close()

# Patch db module path
import db as _db_module
_db_module.DB_PATH = _tmp_db_path

from fastapi.testclient import TestClient

from app import app
from auth import get_current_user, TenantContext
from db import get_db, init_db


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _init_test_database():
    """Run migrations once for the whole test session."""
    init_db()
    yield
    # Cleanup temp file after all tests
    try:
        _tmp_db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            p = _tmp_db_path.with_name(_tmp_db_path.name + suffix)
            p.unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_tables():
    """Truncate mutable tables after each test so tests are isolated."""
    yield
    with get_db() as conn:
        for table in (
            "chores", "completions", "meals", "meal_duties", "adhoc_tasks",
            "calendar_events", "calendar_event_exceptions",
            "bills", "inventory_items", "polls", "poll_options",
            "poll_votes", "push_subscriptions", "settings",
            "shopping_items", "magic_links", "kiosk_tokens",
            "feedback_votes", "feedback",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


@pytest.fixture(autouse=True)
def _clear_overrides():
    """Ensure dependency overrides are cleaned up between tests."""
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Auth / tenant fixtures
# ---------------------------------------------------------------------------

def _make_tenant(user_id: int, household_id: int, name: str, role: str):
    return TenantContext(
        user_id=user_id,
        household_id=household_id,
        display_name=name,
        role=role,
    )


@pytest.fixture()
def tenant_h1():
    """Manager in household 1."""
    return _make_tenant(user_id=1, household_id=1, name="TestUser", role="manager")


@pytest.fixture()
def tenant_h2():
    """Manager in household 2 — for isolation tests."""
    return _make_tenant(user_id=10, household_id=2, name="OtherUser", role="manager")


# ---------------------------------------------------------------------------
# Test clients
# ---------------------------------------------------------------------------

def _set_auth(tenant: TenantContext):
    """Set the auth override to return the given tenant."""
    async def _override():
        return tenant
    app.dependency_overrides[get_current_user] = _override


@pytest.fixture()
def client(tenant_h1):
    """TestClient authenticated as household-1 manager."""
    _set_auth(tenant_h1)
    yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def client_h2(tenant_h2):
    """TestClient authenticated as household-2 manager."""
    _set_auth(tenant_h2)
    yield TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Household / user seed data
# ---------------------------------------------------------------------------

@pytest.fixture()
def seed_households():
    """Ensure both test households exist in the DB."""
    with get_db() as conn:
        # Household 1 is created by the migration seeder.
        # Create household 2 for isolation tests.
        existing = conn.execute(
            "SELECT id FROM households WHERE id = 2"
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO households (id, name, slug, tier, timezone, created_at) "
                "VALUES (2, 'Other House', 'other-house', 'free', 'Australia/Sydney', CURRENT_TIMESTAMP)"
            )
            conn.commit()

        # Ensure users exist for both households
        for uid, email, name in [(1, "test@test.com", "TestUser"), (10, "other@test.com", "OtherUser")]:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, email, display_name, created_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (uid, email, name),
            )
        conn.commit()

        # Ensure household_members
        for hid, uid, name, role in [(1, 1, "TestUser", "manager"), (2, 10, "OtherUser", "manager")]:
            conn.execute(
                "INSERT OR IGNORE INTO household_members (household_id, user_id, display_name, role, joined_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (hid, uid, name, role),
            )
        conn.commit()

    yield


@pytest.fixture()
def seed_settings():
    """Seed minimal settings required by some endpoints."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
            ("household_members", '["TestUser","MemberTwo"]', 1),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
            ("member_colors", '{"TestUser":"#4ecdc4","MemberTwo":"#ff6b9d"}', 1),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
            ("timezone", "Australia/Sydney", 1),
        )
        conn.commit()
