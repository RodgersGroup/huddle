import logging
import os
import sqlite3
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext
from datetime import datetime
from db import get_db
from settings import get_people
from websocket import manager
from push import send_push_to_person_bg

logger = logging.getLogger("huddle")

router = APIRouter()

TASK_PHOTO_DIR = Path(__file__).parent.parent / "static" / "task_photos"
TASK_PHOTO_MAX_SIZE = 10 * 1024 * 1024  # 10MB


def get_task_photo_url(task_id: int) -> str:
    """Return photo URL if the task has a photo on disk."""
    photo_path = TASK_PHOTO_DIR / f"{task_id}.jpg"
    if photo_path.exists():
        return f"/static/task_photos/{task_id}.jpg"
    return ""


def _enrich_task(task_dict: dict) -> dict:
    """Add photo_url to a task dict."""
    photo_url = get_task_photo_url(task_dict["id"])
    if photo_url:
        task_dict["photo_url"] = photo_url
    return task_dict


def _delete_task_photo(task_id: int):
    """Delete a task's photo file if it exists."""
    photo_path = TASK_PHOTO_DIR / f"{task_id}.jpg"
    if photo_path.exists():
        try:
            photo_path.unlink()
        except OSError as e:
            logger.warning("Failed to delete task photo %s: %s", photo_path, e)


@router.get("/api/adhoc")
def get_adhoc(tenant: TenantContext = Depends(get_current_user)):
    """Get all adhoc tasks, incomplete first."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM adhoc_tasks
                WHERE household_id = ?
                ORDER BY completed ASC, created_at DESC
            """, (household_id,)).fetchall()
            return {"tasks": [_enrich_task(dict(r)) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_adhoc: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_adhoc: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/adhoc")
async def create_adhoc(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Create an adhoc task."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
        if not name:
            logger.warning("Validation failed in create_adhoc: %s", "Task name is required")
            raise HTTPException(status_code=400, detail="Task name is required")
        if len(name) > 200:
            logger.warning("Validation failed in create_adhoc: %s", "Task name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Task name is too long (max 200 characters)")

        description = data.get('description', '')
        if isinstance(description, str) and len(description) > 1000:
            logger.warning("Validation failed in create_adhoc: %s", "Description is too long (max 1000 characters)")
            raise HTTPException(status_code=400, detail="Description is too long (max 1000 characters)")
        # --- End validation ---

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO adhoc_tasks (name, description, added_by, task_type, assigned_to, household_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                name,
                data.get('description', ''),
                data.get('added_by'),
                data.get('task_type', 'request'),
                data.get('assigned_to'),
                household_id
            ))
            conn.commit()
            task_id = cursor.lastrowid

        await manager.broadcast({"type": "adhoc_created", "task_id": task_id}, household_id=household_id)

        # Push notification
        task_type = data.get('task_type', 'request')
        added_by = data.get('added_by')
        assigned_to = data.get('assigned_to')

        if task_type == 'request':
            if assigned_to and assigned_to in get_people(household_id=household_id):
                send_push_to_person_bg(assigned_to, "Huddle: New task", f"{added_by or 'Someone'}: {name}", "new-task", household_id=household_id, module="adhoc_tasks")
            else:
                for person in get_people(household_id=household_id):
                    if person != added_by:
                        send_push_to_person_bg(person, "Huddle: New task", f"{added_by or 'Someone'}: {name}", "new-task", household_id=household_id, module="adhoc_tasks")

        return {"id": task_id, "message": "Task created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_adhoc: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_adhoc: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/adhoc/{task_id}/photo")
async def upload_task_photo(task_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Upload a photo for an adhoc task. Accepts multipart form with 'file' field."""
    household_id = tenant.household_id
    try:
        # Verify the task exists and belongs to this household
        with get_db() as conn:
            task = conn.execute(
                "SELECT id FROM adhoc_tasks WHERE id = ? AND household_id = ?",
                (task_id, household_id)
            ).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")

        form = await request.form()
        file = form.get("file")
        if not file:
            raise HTTPException(status_code=400, detail="No file uploaded")

        content = await file.read()
        if len(content) > TASK_PHOTO_MAX_SIZE:
            raise HTTPException(status_code=400, detail="Image too large (max 10MB)")

        try:
            from PIL import Image, ImageOps
            import io
            img = Image.open(io.BytesIO(content))
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")

            # Resize to max 1024px on longest side, preserving aspect ratio
            max_dim = 1024
            w, h = img.size
            if w > max_dim or h > max_dim:
                if w > h:
                    new_w, new_h = max_dim, int(h * max_dim / w)
                else:
                    new_w, new_h = int(w * max_dim / h), max_dim
                img = img.resize((new_w, new_h), Image.LANCZOS)

            # Save as JPEG
            TASK_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
            out_path = TASK_PHOTO_DIR / f"{task_id}.jpg"
            img.save(out_path, "JPEG", quality=85)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid image: {e}")

        # Update DB with photo path
        with get_db() as conn:
            conn.execute(
                "UPDATE adhoc_tasks SET photo_path = ? WHERE id = ? AND household_id = ?",
                (f"{task_id}.jpg", task_id, household_id)
            )
            conn.commit()

        photo_url = f"/static/task_photos/{task_id}.jpg"
        await manager.broadcast({"type": "adhoc_created", "task_id": task_id}, household_id=household_id)
        return {"ok": True, "photo_url": photo_url}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in upload_task_photo: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in upload_task_photo: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/adhoc/{task_id}/photo")
async def delete_task_photo(task_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a task's photo."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            task = conn.execute(
                "SELECT id FROM adhoc_tasks WHERE id = ? AND household_id = ?",
                (task_id, household_id)
            ).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")

            conn.execute(
                "UPDATE adhoc_tasks SET photo_path = NULL WHERE id = ? AND household_id = ?",
                (task_id, household_id)
            )
            conn.commit()

        _delete_task_photo(task_id)
        await manager.broadcast({"type": "adhoc_created", "task_id": task_id}, household_id=household_id)
        return {"ok": True}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_task_photo: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_task_photo: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/adhoc/{task_id}/complete")
async def complete_adhoc(task_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Mark an adhoc task as complete."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        completed_by = data.get('completed_by')
        with get_db() as conn:
            # Fetch task details before updating (for notification)
            task = conn.execute(
                "SELECT * FROM adhoc_tasks WHERE id = ? AND household_id = ?",
                (task_id, household_id)
            ).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")

            conn.execute("""
                UPDATE adhoc_tasks SET completed = 1, completed_by = ?, completed_at = ?
                WHERE id = ? AND household_id = ?
            """, (completed_by, datetime.now().isoformat(), task_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "adhoc_completed", "task_id": task_id}, household_id=household_id)

        # Push notifications for task completion
        task_name = task["name"]
        assigned_to = task["assigned_to"]
        added_by = task["added_by"]
        notified = {completed_by} if completed_by else set()

        # Notify the person the task was assigned to (if someone else completed it)
        if assigned_to and assigned_to != completed_by and assigned_to in get_people(household_id=household_id):
            send_push_to_person_bg(
                assigned_to,
                f"Huddle: {completed_by} helped out!",
                f"{completed_by} completed '{task_name}' for you.",
                "task-help",
                household_id=household_id,
                module="adhoc_tasks",
            )
            notified.add(assigned_to)

        # Notify the person who created the task (if different from completer and assignee)
        if added_by and added_by not in notified and added_by in get_people(household_id=household_id):
            send_push_to_person_bg(
                added_by,
                "Huddle: Task done",
                f"{completed_by} completed '{task_name}'",
                f"task-done-{task_id}",
                household_id=household_id,
                module="adhoc_tasks",
            )

        return {"message": "Task completed"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in complete_adhoc: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in complete_adhoc: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/adhoc/{task_id}")
async def delete_adhoc(task_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete an adhoc task and its photo if present."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute("DELETE FROM adhoc_tasks WHERE id = ? AND household_id = ?", (task_id, household_id))
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Task not found")

        # Clean up photo file
        _delete_task_photo(task_id)

        await manager.broadcast({"type": "adhoc_deleted", "task_id": task_id}, household_id=household_id)
        return {"message": "Task deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_adhoc: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_adhoc: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/adhoc/completed")
async def clear_completed(tenant: TenantContext = Depends(get_current_user)):
    """Delete all completed adhoc tasks and their photos."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            # Get IDs of completed tasks to clean up photos
            completed = conn.execute(
                "SELECT id FROM adhoc_tasks WHERE completed = 1 AND household_id = ?",
                (household_id,)
            ).fetchall()
            conn.execute("DELETE FROM adhoc_tasks WHERE completed = 1 AND household_id = ?", (household_id,))
            conn.commit()

        # Clean up photo files
        for row in completed:
            _delete_task_photo(row["id"])

        await manager.broadcast({"type": "adhoc_cleared"}, household_id=household_id)
        return {"message": "Completed tasks cleared"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in clear_completed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in clear_completed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
