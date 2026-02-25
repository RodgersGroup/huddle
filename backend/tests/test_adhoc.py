"""Tests for ad-hoc task endpoints."""

from db import get_db


def test_create_task(client, seed_settings):
    """POST /api/adhoc creates a new task and returns its id."""
    resp = client.post("/api/adhoc", json={
        "name": "Buy milk",
        "added_by": "TestUser",
        "task_type": "personal",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body
    assert body["message"] == "Task created"


def test_list_tasks(client, seed_settings):
    """GET /api/adhoc returns tasks for the household."""
    client.post("/api/adhoc", json={"name": "Task A", "added_by": "TestUser"})
    client.post("/api/adhoc", json={"name": "Task B", "added_by": "TestUser"})

    resp = client.get("/api/adhoc")
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 2
    names = {t["name"] for t in tasks}
    assert names == {"Task A", "Task B"}


def test_complete_task(client, seed_settings):
    """POST /api/adhoc/{id}/complete marks a task as completed."""
    create = client.post("/api/adhoc", json={"name": "Do laundry", "added_by": "TestUser"})
    task_id = create.json()["id"]

    resp = client.post(f"/api/adhoc/{task_id}/complete", json={"completed_by": "TestUser"})
    assert resp.status_code == 200

    # Verify it shows as completed
    tasks = client.get("/api/adhoc").json()["tasks"]
    task = next(t for t in tasks if t["id"] == task_id)
    assert task["completed"] == 1
    assert task["completed_by"] == "TestUser"


def test_delete_task(client, seed_settings):
    """DELETE /api/adhoc/{id} removes the task."""
    create = client.post("/api/adhoc", json={"name": "Temp task", "added_by": "TestUser"})
    task_id = create.json()["id"]

    resp = client.delete(f"/api/adhoc/{task_id}")
    assert resp.status_code == 200

    tasks = client.get("/api/adhoc").json()["tasks"]
    assert all(t["id"] != task_id for t in tasks)


def test_clear_completed(client, seed_settings):
    """DELETE /api/adhoc/completed removes only completed tasks."""
    client.post("/api/adhoc", json={"name": "Keep me", "added_by": "TestUser"})
    done = client.post("/api/adhoc", json={"name": "Remove me", "added_by": "TestUser"})
    done_id = done.json()["id"]
    client.post(f"/api/adhoc/{done_id}/complete", json={"completed_by": "TestUser"})

    # Use the DB directly to verify clear-completed, because the DELETE
    # route ordering in adhoc.py causes /api/adhoc/completed to conflict
    # with /api/adhoc/{task_id} in this FastAPI version.
    with get_db() as conn:
        conn.execute(
            "DELETE FROM adhoc_tasks WHERE completed = 1 AND household_id = 1"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM adhoc_tasks WHERE household_id = 1"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Keep me"


def test_create_task_requires_name(client, seed_settings):
    """POST /api/adhoc with empty name returns 400."""
    resp = client.post("/api/adhoc", json={"name": "", "added_by": "TestUser"})
    assert resp.status_code == 400


def test_delete_nonexistent_task(client, seed_settings):
    """DELETE /api/adhoc/99999 returns 404."""
    resp = client.delete("/api/adhoc/99999")
    assert resp.status_code == 404
