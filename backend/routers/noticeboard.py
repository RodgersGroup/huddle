import logging
import sqlite3
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext
from db import get_db
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()

VALID_POST_TYPES = {"notice", "going_out", "guest", "reminder", "urgent"}
VALID_PRIORITIES = {"normal", "high", "urgent"}


@router.get("/api/noticeboard")
def list_posts(tenant: TenantContext = Depends(get_current_user)):
    """List all noticeboard posts (pinned first, then newest)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            now = datetime.now().isoformat()
            rows = conn.execute("""
                SELECT * FROM noticeboard_posts
                WHERE household_id = ? AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY pinned DESC, created_at DESC
            """, (household_id, now)).fetchall()
            return {"posts": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in list_posts: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in list_posts: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/noticeboard")
async def create_post(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Create a noticeboard post."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        title = data.get("title", "").strip() if isinstance(data.get("title"), str) else ""
        if not title:
            logger.warning("Validation failed in create_post: title required")
            raise HTTPException(status_code=400, detail="Title is required")
        if len(title) > 200:
            raise HTTPException(status_code=400, detail="Title too long (max 200)")

        body = data.get("body", "").strip() if isinstance(data.get("body"), str) else ""
        if len(body) > 2000:
            raise HTTPException(status_code=400, detail="Body too long (max 2000)")

        post_type = data.get("post_type", "notice")
        if post_type not in VALID_POST_TYPES:
            post_type = "notice"

        priority = data.get("priority", "normal")
        if priority not in VALID_PRIORITIES:
            priority = "normal"

        now = datetime.now().isoformat()
        expires_at = data.get("expires_at")

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO noticeboard_posts (title, body, post_type, priority, posted_by, pinned, expires_at, created_at, updated_at, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, body, post_type, priority, tenant.display_name, 0, expires_at, now, now, household_id))
            conn.commit()
            post_id = cursor.lastrowid

        await manager.broadcast({"type": "noticeboard_updated"}, household_id=household_id)

        # Push notification for urgent/guest posts
        if post_type in ("urgent", "guest", "going_out"):
            try:
                from push import send_push_to_all
                type_labels = {"urgent": "Urgent", "guest": "Guest coming", "going_out": "Going out"}
                await send_push_to_all(
                    f"Huddle: {type_labels.get(post_type, 'Notice')}: {title}",
                    body[:100] if body else title,
                    tag=f"noticeboard-{post_id}",
                    household_id=household_id,
                    module="noticeboard",
                )
            except Exception as push_err:
                logger.warning("Noticeboard push failed: %s", push_err)

        return {"id": post_id, "message": "Post created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_post: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_post: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/noticeboard/{post_id}")
async def update_post(post_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update a noticeboard post (own posts or manager)."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM noticeboard_posts WHERE id = ? AND household_id = ?",
                (post_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Post not found")

            # Only own posts or manager can edit
            if existing["posted_by"] != tenant.display_name and tenant.role != "manager":
                raise HTTPException(status_code=403, detail="Not authorised")

            title = data.get("title", existing["title"])
            if isinstance(title, str):
                title = title.strip()
            if not title:
                raise HTTPException(status_code=400, detail="Title is required")

            body = data.get("body", existing["body"])
            if isinstance(body, str):
                body = body.strip()

            pinned = data.get("pinned", existing["pinned"])
            if "pinned" in data and tenant.role != "manager":
                pinned = existing["pinned"]  # Only managers can pin

            now = datetime.now().isoformat()
            conn.execute("""
                UPDATE noticeboard_posts
                SET title=?, body=?, post_type=?, priority=?, pinned=?, expires_at=?, updated_at=?
                WHERE id=? AND household_id=?
            """, (
                title, body,
                data.get("post_type", existing["post_type"]),
                data.get("priority", existing["priority"]),
                1 if pinned else 0,
                data.get("expires_at", existing["expires_at"]),
                now, post_id, household_id
            ))
            conn.commit()

        await manager.broadcast({"type": "noticeboard_updated"}, household_id=household_id)
        return {"message": "Post updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_post: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_post: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/noticeboard/{post_id}")
async def delete_post(post_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a noticeboard post (own posts or manager)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT posted_by FROM noticeboard_posts WHERE id = ? AND household_id = ?",
                (post_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Post not found")

            if existing["posted_by"] != tenant.display_name and tenant.role != "manager":
                raise HTTPException(status_code=403, detail="Not authorised")

            conn.execute("DELETE FROM noticeboard_posts WHERE id = ? AND household_id = ?", (post_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "noticeboard_updated"}, household_id=household_id)
        return {"message": "Post deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_post: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_post: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/noticeboard/{post_id}/pin")
async def toggle_pin(post_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Toggle pin status (manager only)."""
    household_id = tenant.household_id
    if tenant.role != "manager":
        raise HTTPException(status_code=403, detail="Manager only")
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT pinned FROM noticeboard_posts WHERE id = ? AND household_id = ?",
                (post_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Post not found")

            new_val = 0 if existing["pinned"] else 1
            conn.execute(
                "UPDATE noticeboard_posts SET pinned = ?, updated_at = ? WHERE id = ? AND household_id = ?",
                (new_val, datetime.now().isoformat(), post_id, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "noticeboard_updated"}, household_id=household_id)
        return {"pinned": bool(new_val)}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in toggle_pin: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in toggle_pin: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
