import logging
import sqlite3
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext
from db import get_db, check_version
from websocket import manager
from push import send_push_to_person_bg

logger = logging.getLogger("huddle")

router = APIRouter()

VALID_STATUSES = {"todo", "in_progress", "done"}
VALID_PRIORITIES = {"low", "normal", "high", "urgent"}


@router.get("/api/assignments")
def list_assignments(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """List assignments, optionally filtered by person or status."""
    household_id = tenant.household_id
    try:
        person = request.query_params.get("person")
        status = request.query_params.get("status")

        with get_db() as conn:
            query = "SELECT * FROM assignments WHERE household_id = ? AND deleted_at IS NULL"
            params = [household_id]

            if person:
                query += " AND assigned_to = ?"
                params.append(person)
            if status and status in VALID_STATUSES:
                query += " AND status = ?"
                params.append(status)

            query += " ORDER BY CASE WHEN status = 'done' THEN 1 ELSE 0 END, due_date ASC NULLS LAST, created_at DESC"
            rows = conn.execute(query, params).fetchall()
            return {"assignments": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in list_assignments: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in list_assignments: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/assignments")
async def create_assignment(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Create an assignment."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        title = data.get("title", "").strip() if isinstance(data.get("title"), str) else ""
        if not title:
            raise HTTPException(status_code=400, detail="Title is required")
        if len(title) > 200:
            raise HTTPException(status_code=400, detail="Title too long (max 200)")

        assigned_to = data.get("assigned_to", "").strip() if isinstance(data.get("assigned_to"), str) else ""
        if not assigned_to:
            assigned_to = tenant.display_name

        now = datetime.now().isoformat()

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO assignments (title, description, subject, assigned_to, due_date, due_time, status, priority, notes, created_at, updated_at, household_id)
                VALUES (?, ?, ?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?)
            """, (
                title,
                data.get("description", "").strip() if isinstance(data.get("description"), str) else "",
                data.get("subject", "").strip() if isinstance(data.get("subject"), str) else "",
                assigned_to,
                data.get("due_date"),
                data.get("due_time"),
                data.get("priority", "normal") if data.get("priority") in VALID_PRIORITIES else "normal",
                data.get("notes", "").strip() if isinstance(data.get("notes"), str) else "",
                now, now, household_id
            ))
            conn.commit()
            assignment_id = cursor.lastrowid

        await manager.broadcast({"type": "assignments_updated"}, household_id=household_id)

        # Notify the assigned person if it's not the creator
        if assigned_to != tenant.display_name:
            try:
                due_text = f" (due {data.get('due_date')})" if data.get('due_date') else ""
                send_push_to_person_bg(
                    assigned_to,
                    "Huddle: New assignment",
                    f"{tenant.display_name} assigned you: {title}{due_text}",
                    "assignment-new",
                    household_id=household_id,
                    module="assignments",
                )
            except Exception as push_err:
                logger.warning("Assignment push notification failed: %s", push_err)

        return {"id": assignment_id, "message": "Assignment created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_assignment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_assignment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/assignments/{assignment_id}")
async def update_assignment(assignment_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update an assignment."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM assignments WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (assignment_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Assignment not found")
            check_version(data, existing, "assignment")

            now = datetime.now().isoformat()
            new_status = data.get("status", existing["status"])
            completed_at = existing["completed_at"]
            if new_status == "done" and existing["status"] != "done":
                completed_at = now
            elif new_status != "done":
                completed_at = None

            conn.execute("""
                UPDATE assignments SET title=?, description=?, subject=?, assigned_to=?,
                due_date=?, due_time=?, status=?, priority=?, notes=?, updated_at=?, completed_at=?,
                version = COALESCE(version, 0) + 1
                WHERE id=? AND household_id=?
            """, (
                data.get("title", existing["title"]),
                data.get("description", existing["description"]),
                data.get("subject", existing["subject"]),
                data.get("assigned_to", existing["assigned_to"]),
                data.get("due_date", existing["due_date"]),
                data.get("due_time", existing["due_time"]),
                new_status if new_status in VALID_STATUSES else existing["status"],
                data.get("priority", existing["priority"]) if data.get("priority") in VALID_PRIORITIES else existing["priority"],
                data.get("notes", existing["notes"]),
                now, completed_at, assignment_id, household_id
            ))
            conn.commit()

        await manager.broadcast({"type": "assignments_updated"}, household_id=household_id)
        return {"message": "Assignment updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_assignment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_assignment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/assignments/{assignment_id}")
async def delete_assignment(assignment_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete an assignment."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute(
                "UPDATE assignments SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?",
                (assignment_id, household_id)
            )
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Assignment not found")

        await manager.broadcast({"type": "assignments_updated"}, household_id=household_id)
        return {"message": "Assignment deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_assignment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_assignment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/assignments/{assignment_id}/complete")
async def complete_assignment(assignment_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Mark an assignment as done."""
    household_id = tenant.household_id
    try:
        now = datetime.now().isoformat()
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM assignments WHERE id = ? AND household_id = ?",
                (assignment_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Assignment not found")

            conn.execute(
                "UPDATE assignments SET status = 'done', completed_at = ?, updated_at = ? WHERE id = ? AND household_id = ?",
                (now, now, assignment_id, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "assignments_updated"}, household_id=household_id)
        return {"message": "Assignment completed"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in complete_assignment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in complete_assignment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
