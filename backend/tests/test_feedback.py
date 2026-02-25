"""Tests for the feedback API endpoints."""

from tests.conftest import _set_auth, _make_tenant
from fastapi.testclient import TestClient
from app import app


# --- Create feedback (all types) ---

def test_create_feedback_bug(client, seed_households):
    resp = client.post("/api/feedback", json={
        "feedback_type": "bug",
        "title": "Calendar events not loading",
        "description": "When I open the calendar on Wednesdays, no events show.",
        "page_context": "calendar",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] is not None
    assert data["message"] == "Thanks for your feedback!"


def test_create_feedback_feature(client, seed_households):
    resp = client.post("/api/feedback", json={
        "feedback_type": "feature",
        "title": "Add dark mode",
    })
    assert resp.status_code == 200
    assert resp.json()["id"] is not None


def test_create_feedback_general(client, seed_households):
    resp = client.post("/api/feedback", json={
        "feedback_type": "general",
        "title": "Love the app!",
        "description": "Works great for our household.",
    })
    assert resp.status_code == 200


def test_create_feedback_praise(client, seed_households):
    resp = client.post("/api/feedback", json={
        "feedback_type": "praise",
        "title": "Chore rotation is brilliant",
    })
    assert resp.status_code == 200


# --- List with filters ---

def test_list_feedback(client, seed_households):
    client.post("/api/feedback", json={"feedback_type": "bug", "title": "Bug 1"})
    client.post("/api/feedback", json={"feedback_type": "feature", "title": "Feature 1"})
    client.post("/api/feedback", json={"feedback_type": "general", "title": "General 1"})

    resp = client.get("/api/feedback")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3
    assert data["counts"]["total"] == 3
    assert data["counts"]["new"] == 3


def test_list_feedback_filtered(client, seed_households):
    client.post("/api/feedback", json={"feedback_type": "bug", "title": "Bug 1"})
    client.post("/api/feedback", json={"feedback_type": "feature", "title": "Feature 1"})

    resp = client.get("/api/feedback?type=bug")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["feedback_type"] == "bug"


def test_list_feedback_invalid_filter(client, seed_households):
    resp = client.get("/api/feedback?type=invalid")
    assert resp.status_code == 400


# --- Vote toggle ---

def test_vote_toggle(client, seed_households):
    resp = client.post("/api/feedback", json={"feedback_type": "bug", "title": "Bug"})
    fb_id = resp.json()["id"]

    # Vote
    resp = client.post(f"/api/feedback/{fb_id}/vote")
    assert resp.status_code == 200
    data = resp.json()
    assert data["voted"] is True
    assert data["vote_count"] == 1

    # Unvote
    resp = client.post(f"/api/feedback/{fb_id}/vote")
    assert resp.status_code == 200
    data = resp.json()
    assert data["voted"] is False
    assert data["vote_count"] == 0

    # Re-vote
    resp = client.post(f"/api/feedback/{fb_id}/vote")
    assert resp.status_code == 200
    assert resp.json()["voted"] is True


def test_vote_not_found(client, seed_households):
    resp = client.post("/api/feedback/99999/vote")
    assert resp.status_code == 404


# --- Get single feedback ---

def test_get_feedback(client, seed_households):
    resp = client.post("/api/feedback", json={"feedback_type": "bug", "title": "Test bug"})
    fb_id = resp.json()["id"]

    resp = client.get(f"/api/feedback/{fb_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Test bug"
    assert data["feedback_type"] == "bug"
    assert data["user_voted"] is False
    assert data["voters"] == []


def test_get_feedback_not_found(client, seed_households):
    resp = client.get("/api/feedback/99999")
    assert resp.status_code == 404


# --- Edit own feedback ---

def test_edit_own_feedback(client, seed_households):
    resp = client.post("/api/feedback", json={"feedback_type": "bug", "title": "Old Title"})
    fb_id = resp.json()["id"]

    resp = client.put(f"/api/feedback/{fb_id}", json={
        "title": "New Title",
        "description": "Added details",
    })
    assert resp.status_code == 200

    item = client.get(f"/api/feedback/{fb_id}").json()
    assert item["title"] == "New Title"
    assert item["description"] == "Added details"


def test_cannot_edit_others_feedback(seed_households):
    """Members cannot edit other members' feedback."""
    t1 = _make_tenant(user_id=1, household_id=1, name="TestUser", role="member")
    t2 = _make_tenant(user_id=2, household_id=1, name="MemberTwo", role="member")

    _set_auth(t1)
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/api/feedback", json={"feedback_type": "bug", "title": "My Bug"})
    fb_id = resp.json()["id"]

    _set_auth(t2)
    resp = c.put(f"/api/feedback/{fb_id}", json={"title": "Hacked"})
    assert resp.status_code == 403


# --- Manager status update ---

def test_manager_status_update(client, seed_households):
    resp = client.post("/api/feedback", json={"feedback_type": "bug", "title": "Needs fix"})
    fb_id = resp.json()["id"]

    resp = client.put(f"/api/feedback/{fb_id}", json={
        "status": "acknowledged",
        "admin_notes": "Looking into this",
    })
    assert resp.status_code == 200

    item = client.get(f"/api/feedback/{fb_id}").json()
    assert item["status"] == "acknowledged"
    assert item["admin_notes"] == "Looking into this"


def test_manager_invalid_status(client, seed_households):
    resp = client.post("/api/feedback", json={"feedback_type": "bug", "title": "Bug"})
    fb_id = resp.json()["id"]

    resp = client.put(f"/api/feedback/{fb_id}", json={"status": "invalid_status"})
    assert resp.status_code == 400


def test_member_cannot_change_status(seed_households):
    """Non-manager members cannot change status."""
    t_manager = _make_tenant(user_id=1, household_id=1, name="TestUser", role="manager")
    t_member = _make_tenant(user_id=2, household_id=1, name="MemberTwo", role="member")

    _set_auth(t_manager)
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/api/feedback", json={"feedback_type": "bug", "title": "Bug"})
    fb_id = resp.json()["id"]

    _set_auth(t_member)
    # Member tries to change status — should get 400 "No valid fields"
    # because member can't update status and it's not their own feedback
    resp = c.put(f"/api/feedback/{fb_id}", json={"status": "resolved"})
    assert resp.status_code == 403


def test_owner_cannot_edit_after_status_change(client, seed_households):
    """Owner can't edit title once status moves past 'new'."""
    resp = client.post("/api/feedback", json={"feedback_type": "bug", "title": "Original"})
    fb_id = resp.json()["id"]

    # Manager changes status
    client.put(f"/api/feedback/{fb_id}", json={"status": "acknowledged"})

    # Now try editing title as a non-manager owner
    t_member = _make_tenant(user_id=1, household_id=1, name="TestUser", role="member")
    _set_auth(t_member)
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.put(f"/api/feedback/{fb_id}", json={"title": "Changed"})
    # Should fail — status is no longer 'new', and member can't change status
    assert resp.status_code == 400


# --- Delete ---

def test_delete_own_feedback(client, seed_households):
    resp = client.post("/api/feedback", json={"feedback_type": "bug", "title": "To Delete"})
    fb_id = resp.json()["id"]

    resp = client.delete(f"/api/feedback/{fb_id}")
    assert resp.status_code == 200

    resp = client.get(f"/api/feedback/{fb_id}")
    assert resp.status_code == 404


def test_manager_delete_others(seed_households):
    """Managers can delete any feedback."""
    t_member = _make_tenant(user_id=2, household_id=1, name="MemberTwo", role="member")
    t_manager = _make_tenant(user_id=1, household_id=1, name="TestUser", role="manager")

    _set_auth(t_member)
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/api/feedback", json={"feedback_type": "bug", "title": "Member Bug"})
    fb_id = resp.json()["id"]

    _set_auth(t_manager)
    resp = c.delete(f"/api/feedback/{fb_id}")
    assert resp.status_code == 200


def test_member_cannot_delete_others(seed_households):
    """Non-managers cannot delete other people's feedback."""
    t1 = _make_tenant(user_id=1, household_id=1, name="TestUser", role="member")
    t2 = _make_tenant(user_id=2, household_id=1, name="MemberTwo", role="member")

    _set_auth(t1)
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/api/feedback", json={"feedback_type": "bug", "title": "My Bug"})
    fb_id = resp.json()["id"]

    _set_auth(t2)
    resp = c.delete(f"/api/feedback/{fb_id}")
    assert resp.status_code == 403


def test_delete_not_found(client, seed_households):
    resp = client.delete("/api/feedback/99999")
    assert resp.status_code == 404


# --- Validation ---

def test_empty_title(client, seed_households):
    resp = client.post("/api/feedback", json={"feedback_type": "bug", "title": ""})
    assert resp.status_code == 400

    resp = client.post("/api/feedback", json={"feedback_type": "bug", "title": "   "})
    assert resp.status_code == 400


def test_missing_type(client, seed_households):
    resp = client.post("/api/feedback", json={"title": "No type"})
    assert resp.status_code == 400


def test_invalid_type(client, seed_households):
    resp = client.post("/api/feedback", json={"feedback_type": "nonsense", "title": "Bad Type"})
    assert resp.status_code == 400


def test_title_too_long(client, seed_households):
    resp = client.post("/api/feedback", json={
        "feedback_type": "bug",
        "title": "x" * 201,
    })
    assert resp.status_code == 400


def test_description_too_long(client, seed_households):
    resp = client.post("/api/feedback", json={
        "feedback_type": "bug",
        "title": "Valid title",
        "description": "x" * 2001,
    })
    assert resp.status_code == 400


# --- Cross-household isolation ---

def test_household_isolation(seed_households):
    """Feedback from one household should not be visible to another."""
    t1 = _make_tenant(user_id=1, household_id=1, name="TestUser", role="manager")
    t2 = _make_tenant(user_id=10, household_id=2, name="OtherUser", role="manager")

    _set_auth(t1)
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/feedback", json={"feedback_type": "bug", "title": "H1 Bug"})

    _set_auth(t2)
    c.post("/api/feedback", json={"feedback_type": "feature", "title": "H2 Feature"})

    # H1 sees only its feedback
    _set_auth(t1)
    items = c.get("/api/feedback").json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "H1 Bug"

    # H2 sees only its feedback
    _set_auth(t2)
    items = c.get("/api/feedback").json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "H2 Feature"


def test_vote_isolation(seed_households):
    """Votes are scoped to household — can't vote on another household's feedback."""
    t1 = _make_tenant(user_id=1, household_id=1, name="TestUser", role="manager")
    t2 = _make_tenant(user_id=10, household_id=2, name="OtherUser", role="manager")

    _set_auth(t1)
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/api/feedback", json={"feedback_type": "bug", "title": "H1 Bug"})
    fb_id = resp.json()["id"]

    # H2 tries to vote on H1's feedback
    _set_auth(t2)
    resp = c.post(f"/api/feedback/{fb_id}/vote")
    assert resp.status_code == 404
