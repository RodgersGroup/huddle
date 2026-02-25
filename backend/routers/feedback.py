"""Feedback module — beta tester bug reports, feature requests, and general feedback."""

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse
from auth import get_current_user, TenantContext
from db import get_db
from websocket import manager

FEEDBACK_SCREENSHOT_DIR = Path(__file__).parent.parent / "static" / "feedback_screenshots"
FEEDBACK_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_MAX_SIZE = 10 * 1024 * 1024  # 10MB

logger = logging.getLogger("huddle")

router = APIRouter()

VALID_TYPES = {"bug", "feature", "general", "praise"}
VALID_STATUSES = {"new", "acknowledged", "in_progress", "resolved", "wont_fix"}
SUPERADMIN_EMAIL = "keiran@rodgersgroup.au"

# Email rate limiting: track last email sent per household and pending count
_email_cooldown: dict[int, float] = {}  # household_id -> timestamp
_email_pending_count: dict[int, int] = {}  # household_id -> count of items since last email
EMAIL_COOLDOWN_SECONDS = 300  # 5 minutes


def _is_superadmin(conn, user_id: int) -> bool:
    """Check if a user is the superadmin."""
    row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and row["email"] == SUPERADMIN_EMAIL


@router.get("/api/feedback")
def list_feedback(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """List feedback for the household, newest first, with vote counts.

    Visibility rules:
    - Managers see all feedback for their household
    - Non-managers see: features, general, praise from everyone + their own bugs
    - Superadmin with ?all_households=true sees all feedback across all households
    """
    household_id = tenant.household_id
    try:
        feedback_type = request.query_params.get("type")
        if feedback_type and feedback_type not in VALID_TYPES:
            raise HTTPException(status_code=400, detail=f"Invalid feedback type: {feedback_type}")

        is_manager = tenant.role == "manager"

        # Superadmin cross-household view
        all_households = request.query_params.get("all_households") == "true"
        is_superadmin = False
        if all_households:
            with get_db() as conn:
                is_superadmin = _is_superadmin(conn, tenant.user_id)
            if not is_superadmin:
                raise HTTPException(status_code=403, detail="Not authorized")

        with get_db() as conn:
            if all_households and is_superadmin:
                # Superadmin: all feedback across all households with household name
                if feedback_type:
                    rows = conn.execute("""
                        SELECT f.*,
                               COUNT(v.id) as vote_count,
                               h.name as household_name
                        FROM feedback f
                        LEFT JOIN feedback_votes v ON v.feedback_id = f.id
                        LEFT JOIN households h ON h.id = f.household_id
                        WHERE f.feedback_type = ?
                        GROUP BY f.id
                        ORDER BY f.created_at DESC
                    """, (feedback_type,)).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT f.*,
                               COUNT(v.id) as vote_count,
                               h.name as household_name
                        FROM feedback f
                        LEFT JOIN feedback_votes v ON v.feedback_id = f.id
                        LEFT JOIN households h ON h.id = f.household_id
                        GROUP BY f.id
                        ORDER BY f.created_at DESC
                    """).fetchall()
            elif feedback_type:
                rows = conn.execute("""
                    SELECT f.*,
                           COUNT(v.id) as vote_count
                    FROM feedback f
                    LEFT JOIN feedback_votes v ON v.feedback_id = f.id
                    WHERE f.household_id = ? AND f.feedback_type = ?
                    GROUP BY f.id
                    ORDER BY f.created_at DESC
                """, (household_id, feedback_type)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT f.*,
                           COUNT(v.id) as vote_count
                    FROM feedback f
                    LEFT JOIN feedback_votes v ON v.feedback_id = f.id
                    WHERE f.household_id = ?
                    GROUP BY f.id
                    ORDER BY f.created_at DESC
                """, (household_id,)).fetchall()

            # Check which items the current user has voted on
            user_votes = set()
            if all_households and is_superadmin:
                # For cross-household view, get all votes by this user across households
                vote_rows = conn.execute(
                    "SELECT feedback_id FROM feedback_votes WHERE voter = ?",
                    (tenant.display_name,)
                ).fetchall()
            else:
                vote_rows = conn.execute(
                    "SELECT feedback_id FROM feedback_votes WHERE household_id = ? AND voter = ?",
                    (household_id, tenant.display_name)
                ).fetchall()
            for vr in vote_rows:
                user_votes.add(vr["feedback_id"])

            items = []
            for r in rows:
                item = dict(r)
                # Hide bugs from non-managers (unless it's their own) — skip for superadmin all-households view
                if not all_households and not is_manager and item["feedback_type"] == "bug" and item["submitted_by"] != tenant.display_name:
                    continue
                item["user_voted"] = item["id"] in user_votes
                # Add screenshot_url if screenshot exists
                if item.get("screenshot_path"):
                    item["screenshot_url"] = f"/{item['screenshot_path']}?t={int(time.time())}"
                else:
                    item["screenshot_url"] = None
                items.append(item)

            # Status counts (based on visible items only)
            counts = {s: 0 for s in VALID_STATUSES}
            total = 0
            for item in items:
                counts[item["status"]] = counts.get(item["status"], 0) + 1
                total += 1
            counts["total"] = total

        return {"items": items, "counts": counts}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in list_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in list_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/feedback")
async def create_feedback(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Submit new feedback."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        feedback_type = data.get("feedback_type", "").strip() if isinstance(data.get("feedback_type"), str) else ""
        if not feedback_type:
            logger.warning("Validation failed in create_feedback: feedback_type is required")
            raise HTTPException(status_code=400, detail="feedback_type is required")
        if feedback_type not in VALID_TYPES:
            logger.warning("Validation failed in create_feedback: invalid feedback_type %s", feedback_type)
            raise HTTPException(status_code=400, detail=f"feedback_type must be one of: {', '.join(sorted(VALID_TYPES))}")

        title = data.get("title", "").strip() if isinstance(data.get("title"), str) else ""
        if not title:
            logger.warning("Validation failed in create_feedback: title is required")
            raise HTTPException(status_code=400, detail="Title is required")
        if len(title) > 200:
            logger.warning("Validation failed in create_feedback: title too long")
            raise HTTPException(status_code=400, detail="Title is too long (max 200 characters)")

        description = data.get("description", "")
        if isinstance(description, str) and len(description) > 2000:
            logger.warning("Validation failed in create_feedback: description too long")
            raise HTTPException(status_code=400, detail="Description is too long (max 2000 characters)")

        page_context = data.get("page_context", "")
        # --- End validation ---

        user_agent = request.headers.get("user-agent", "")

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO feedback (feedback_type, title, description, page_context, submitted_by, user_agent, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                feedback_type,
                title,
                description or None,
                page_context or None,
                tenant.display_name,
                user_agent,
                household_id,
            ))
            conn.commit()
            feedback_id = cursor.lastrowid

        await manager.broadcast({"type": "feedback_updated"}, household_id=household_id)

        # Email notification to superadmin (rate-limited)
        _send_feedback_email_rate_limited(feedback_type, title, description, tenant.display_name, household_id, tenant.user_id)

        # ntfy notification (every submission, not rate-limited)
        await _send_ntfy_notification(feedback_type, title, description, tenant.display_name, household_id)

        return {"id": feedback_id, "message": "Thanks for your feedback!"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


def _send_feedback_email_rate_limited(feedback_type: str, title: str, description: str | None, submitted_by: str, household_id: int, user_id: int = None):
    """Send branded email notification to superadmin, with per-household rate limiting."""
    import config as cfg

    # Skip emails in development mode
    if cfg.ENVIRONMENT == "development":
        logger.info("[DEV] Skipping feedback email for: %s", title)
        return

    # Check if feedback emails are enabled
    if not cfg.FEEDBACK_EMAIL_ENABLED:
        logger.debug("Feedback emails disabled, skipping for: %s", title)
        return

    # Rate limiting: 5-minute cooldown per household
    now = time.time()
    last_sent = _email_cooldown.get(household_id, 0)
    if now - last_sent < EMAIL_COOLDOWN_SECONDS:
        _email_pending_count[household_id] = _email_pending_count.get(household_id, 0) + 1
        logger.info("Feedback email rate-limited for household %d (pending: %d), skipping: %s",
                     household_id, _email_pending_count[household_id], title)
        return

    # How many were submitted since last email?
    pending = _email_pending_count.pop(household_id, 0)

    _send_feedback_email(feedback_type, title, description, submitted_by, household_id, user_id, pending)
    _email_cooldown[household_id] = now


def _send_feedback_email(feedback_type: str, title: str, description: str | None, submitted_by: str, household_id: int, user_id: int = None, pending_count: int = 0):
    """Send branded email notification to superadmin when new feedback is submitted."""
    try:
        import resend
        from config import RESEND_API_KEY

        resend_api_key = RESEND_API_KEY
        if not resend_api_key:
            return

        resend.api_key = resend_api_key

        # Look up user email and household name
        user_email = ""
        household_name = ""
        try:
            with get_db() as conn:
                if user_id:
                    user_row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
                    if user_row:
                        user_email = user_row["email"]
                hh_row = conn.execute("SELECT name FROM households WHERE id = ?", (household_id,)).fetchone()
                if hh_row:
                    household_name = hh_row["name"]
        except Exception:
            pass

        type_labels = {"bug": "Bug Report", "feature": "Feature Request", "general": "General Feedback", "praise": "Praise"}
        type_label = type_labels.get(feedback_type, feedback_type)
        type_colors = {"bug": "#ff6b6b", "feature": "#4ecdc4", "general": "#7e57c2", "praise": "#4caf50"}
        type_color = type_colors.get(feedback_type, "#4ecdc4")
        type_icons = {"bug": "&#128027;", "feature": "&#128161;", "general": "&#128172;", "praise": "&#11088;"}
        type_icon = type_icons.get(feedback_type, "&#128172;")

        timestamp = datetime.now(timezone.utc).strftime("%d %b %Y at %H:%M UTC")
        desc_html = f'<p style="margin:0 0 4px;color:#999;font-size:13px;font-weight:600;">DETAILS</p><p style="margin:0;color:#e0e0e0;font-size:14px;line-height:1.5;">{description}</p>' if description else ""
        email_line = f'<p style="margin:0 0 4px;color:#999;font-size:13px;font-weight:600;">EMAIL</p><p style="margin:0;color:#e0e0e0;font-size:14px;">{user_email}</p>' if user_email and "@placeholder" not in user_email else ""
        household_line = f'<p style="margin:0 0 4px;color:#999;font-size:13px;font-weight:600;">HOUSEHOLD</p><p style="margin:0;color:#e0e0e0;font-size:14px;">{household_name}</p>' if household_name else ""

        # Subject includes pending count if items were batched
        subject = f"[Huddle] New {type_label}: {title}"
        if pending_count > 0:
            subject = f"[Huddle] New {type_label} (+{pending_count} more): {title}"

        pending_banner = ""
        if pending_count > 0:
            pending_banner = f"""
                <div style="text-align:center;margin-bottom:12px;">
                    <span style="display:inline-block;background:#ff9800;color:#fff;font-size:11px;font-weight:700;padding:4px 12px;border-radius:12px;">
                        +{pending_count} more feedback item{'s' if pending_count > 1 else ''} submitted since last email
                    </span>
                </div>"""

        html = f"""
        <div style="background-color:#1a1a2e;padding:0;margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
            <div style="max-width:560px;margin:0 auto;padding:32px 16px;">

                <!-- Header -->
                <div style="text-align:center;margin-bottom:28px;">
                    <div style="display:inline-block;background:linear-gradient(135deg,#4ecdc4,#44a08d);width:48px;height:48px;border-radius:14px;line-height:48px;font-size:22px;margin-bottom:12px;">&#127968;</div>
                    <h1 style="margin:0;color:#ffffff;font-size:20px;font-weight:700;letter-spacing:-0.3px;">Huddle</h1>
                </div>

                {pending_banner}

                <!-- Badge -->
                <div style="text-align:center;margin-bottom:20px;">
                    <span style="display:inline-block;background:{type_color};color:#fff;font-size:12px;font-weight:700;padding:5px 14px;border-radius:20px;text-transform:uppercase;letter-spacing:0.5px;">
                        {type_icon} {type_label}
                    </span>
                </div>

                <!-- Card -->
                <div style="background-color:#16213e;border-radius:16px;border:1px solid #2a2a4a;overflow:hidden;">
                    <!-- Title bar -->
                    <div style="padding:20px 24px;border-bottom:1px solid #2a2a4a;">
                        <h2 style="margin:0;color:#ffffff;font-size:18px;font-weight:600;line-height:1.3;">{title}</h2>
                        <p style="margin:6px 0 0;color:#888;font-size:13px;">{timestamp}</p>
                    </div>

                    <!-- Details -->
                    <div style="padding:20px 24px;">
                        <table style="width:100%;border-collapse:collapse;">
                            <tr>
                                <td style="padding:0 16px 16px 0;vertical-align:top;width:50%;">
                                    <p style="margin:0 0 4px;color:#999;font-size:13px;font-weight:600;">FROM</p>
                                    <p style="margin:0;color:#e0e0e0;font-size:14px;">{submitted_by}</p>
                                </td>
                                <td style="padding:0 0 16px;vertical-align:top;">
                                    {email_line}
                                </td>
                            </tr>
                            {"<tr><td colspan='2' style='padding:0 0 16px;'>" + household_line + "</td></tr>" if household_line else ""}
                            {"<tr><td colspan='2' style='padding:0 0 16px;'>" + desc_html + "</td></tr>" if desc_html else ""}
                        </table>
                    </div>

                    <!-- CTA -->
                    <div style="padding:0 24px 24px;text-align:center;">
                        <a href="https://huddle.rodgersgroup.au/mobile?module=feedback"
                           style="display:inline-block;background:linear-gradient(135deg,#4ecdc4,#44a08d);color:#fff;text-decoration:none;font-size:14px;font-weight:600;padding:12px 32px;border-radius:10px;">
                            View in Huddle &rarr;
                        </a>
                    </div>
                </div>

                <!-- Footer -->
                <p style="text-align:center;color:#555;font-size:12px;margin-top:24px;">
                    Huddle &middot; Household Management
                </p>
            </div>
        </div>
        """

        resend.Emails.send({
            "from": "Huddle <noreply@huddle.rodgersgroup.au>",
            "to": ["keiran@rodgersgroup.au"],
            "subject": subject,
            "html": html,
        })
        logger.info("Feedback email sent for: %s", title)
    except Exception as e:
        logger.warning("Failed to send feedback email: %s", e)


async def _send_ntfy_notification(feedback_type: str, title: str, description: str | None, submitted_by: str, household_id: int):
    """Send a notification via ntfy.sh for every feedback submission. Fails silently."""
    import config as cfg

    topic = cfg.NTFY_TOPIC
    if not topic:
        return

    server = cfg.NTFY_SERVER
    token = cfg.NTFY_TOKEN

    type_labels = {"bug": "Bug Report", "feature": "Feature Request", "general": "General Feedback", "praise": "Praise"}
    type_label = type_labels.get(feedback_type, feedback_type)

    # ntfy tag emojis
    tag_map = {"bug": "bug,warning", "feature": "bulb", "general": "speech_balloon", "praise": "heart"}
    tags = tag_map.get(feedback_type, "speech_balloon")

    # Priority: bug=4 (high), feature/general=3 (default), praise=2 (low)
    priority_map = {"bug": "4", "feature": "3", "general": "3", "praise": "2"}
    priority = priority_map.get(feedback_type, "3")

    # Look up household name
    household_name = ""
    try:
        with get_db() as conn:
            hh_row = conn.execute("SELECT name FROM households WHERE id = ?", (household_id,)).fetchone()
            if hh_row:
                household_name = hh_row["name"]
    except Exception:
        pass

    ntfy_title = f"Huddle: New {type_label}"
    message = f"{title}"
    if description:
        message += f"\n\n{description[:200]}"
    message += f"\n\nFrom: {submitted_by}"
    if household_name:
        message += f"\nHousehold: {household_name}"

    headers = {
        "Title": ntfy_title,
        "Priority": priority,
        "Tags": tags,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{server}/{topic}",
                content=message,
                headers=headers,
                timeout=5.0,
            )
        logger.info("ntfy notification sent for feedback: %s", title)
    except Exception as e:
        logger.warning("Failed to send ntfy notification: %s", e)


@router.get("/api/feedback/{feedback_id}")
def get_feedback(feedback_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Get a single feedback item with votes."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            # Check if superadmin (can view any household's feedback)
            is_superadmin = _is_superadmin(conn, tenant.user_id)

            if is_superadmin:
                row = conn.execute("""
                    SELECT f.*,
                           COUNT(v.id) as vote_count,
                           h.name as household_name
                    FROM feedback f
                    LEFT JOIN feedback_votes v ON v.feedback_id = f.id
                    LEFT JOIN households h ON h.id = f.household_id
                    WHERE f.id = ?
                    GROUP BY f.id
                """, (feedback_id,)).fetchone()
            else:
                row = conn.execute("""
                    SELECT f.*,
                           COUNT(v.id) as vote_count
                    FROM feedback f
                    LEFT JOIN feedback_votes v ON v.feedback_id = f.id
                    WHERE f.id = ? AND f.household_id = ?
                    GROUP BY f.id
                """, (feedback_id, household_id)).fetchone()

            if not row:
                raise HTTPException(status_code=404, detail="Feedback not found")

            item = dict(row)

            # Check if current user voted
            vote = conn.execute(
                "SELECT 1 FROM feedback_votes WHERE feedback_id = ? AND voter = ?",
                (feedback_id, tenant.display_name)
            ).fetchone()
            item["user_voted"] = vote is not None

            # Get list of voters
            voters = conn.execute(
                "SELECT voter FROM feedback_votes WHERE feedback_id = ?",
                (feedback_id,)
            ).fetchall()
            item["voters"] = [v["voter"] for v in voters]
            if item.get("screenshot_path"):
                item["screenshot_url"] = f"/{item['screenshot_path']}?t={int(time.time())}"
            else:
                item["screenshot_url"] = None

        return item
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/feedback/{feedback_id}")
async def update_feedback(feedback_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update feedback. Members can edit own title/description while status is 'new'. Managers and superadmin can update status and admin_notes."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            is_superadmin = _is_superadmin(conn, tenant.user_id)

            if is_superadmin:
                existing = conn.execute(
                    "SELECT * FROM feedback WHERE id = ?", (feedback_id,)
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT * FROM feedback WHERE id = ? AND household_id = ?",
                    (feedback_id, household_id)
                ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Feedback not found")

            is_owner = existing["submitted_by"] == tenant.display_name
            is_manager = tenant.role == "manager" or is_superadmin

            if not is_owner and not is_manager:
                raise HTTPException(status_code=403, detail="You can only edit your own feedback")

            updates = []
            params = []

            # Owner can edit title/description/type while status is 'new'
            if is_owner and existing["status"] == "new":
                if "title" in data:
                    title = data["title"].strip() if isinstance(data["title"], str) else ""
                    if not title:
                        raise HTTPException(status_code=400, detail="Title is required")
                    if len(title) > 200:
                        raise HTTPException(status_code=400, detail="Title is too long (max 200 characters)")
                    updates.append("title = ?")
                    params.append(title)
                if "description" in data:
                    desc = data["description"] or ""
                    if isinstance(desc, str) and len(desc) > 2000:
                        raise HTTPException(status_code=400, detail="Description is too long (max 2000 characters)")
                    updates.append("description = ?")
                    params.append(desc or None)
                if "feedback_type" in data:
                    new_type = data["feedback_type"].strip() if isinstance(data["feedback_type"], str) else ""
                    if new_type not in VALID_TYPES:
                        raise HTTPException(status_code=400, detail=f"feedback_type must be one of: {', '.join(sorted(VALID_TYPES))}")
                    updates.append("feedback_type = ?")
                    params.append(new_type)

            # Manager can update status and admin_notes
            if is_manager:
                if "status" in data:
                    new_status = data["status"]
                    if new_status not in VALID_STATUSES:
                        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}")
                    old_status = existing["status"]
                    updates.append("status = ?")
                    params.append(new_status)
                if "admin_notes" in data:
                    updates.append("admin_notes = ?")
                    params.append(data["admin_notes"])

            if not updates:
                raise HTTPException(status_code=400, detail="No valid fields to update")

            updates.append("updated_at = datetime('now')")
            feedback_household_id = existing["household_id"]
            if is_superadmin:
                params.append(feedback_id)
                conn.execute(
                    f"UPDATE feedback SET {', '.join(updates)} WHERE id = ?",
                    params
                )
            else:
                params.extend([feedback_id, household_id])
                conn.execute(
                    f"UPDATE feedback SET {', '.join(updates)} WHERE id = ? AND household_id = ?",
                    params
                )
            conn.commit()

            # Send push notification + email on status change to acknowledged or resolved
            if is_manager and "status" in data and data["status"] in ("acknowledged", "resolved"):
                new_status = data["status"]
                submitter = existing["submitted_by"]
                title = existing["title"]
                admin_notes_text = data.get("admin_notes", existing["admin_notes"] or "")
                try:
                    from push import send_push_to_person_bg
                    if new_status == "acknowledged":
                        send_push_to_person_bg(
                            submitter,
                            "Huddle: Feedback Acknowledged",
                            f"Your feedback '{title}' has been acknowledged",
                            tag="feedback",
                            household_id=feedback_household_id,
                        )
                    elif new_status == "resolved":
                        send_push_to_person_bg(
                            submitter,
                            "Huddle: Feedback Resolved",
                            f"Your feedback '{title}' has been resolved",
                            tag="feedback",
                            household_id=feedback_household_id,
                        )
                except Exception as e:
                    logger.warning("Failed to send feedback status push: %s", e)

                # Email the original poster when resolved
                if new_status == "resolved":
                    _send_status_email_to_poster(feedback_id, title, admin_notes_text, submitter, feedback_household_id)

        await manager.broadcast({"type": "feedback_updated"}, household_id=feedback_household_id)
        return {"message": "Feedback updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/feedback/{feedback_id}")
async def delete_feedback(feedback_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete feedback. Own items or manager."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM feedback WHERE id = ? AND household_id = ?",
                (feedback_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Feedback not found")

            is_owner = existing["submitted_by"] == tenant.display_name
            is_manager = tenant.role == "manager"

            if not is_owner and not is_manager:
                raise HTTPException(status_code=403, detail="You can only delete your own feedback")

            conn.execute("DELETE FROM feedback WHERE id = ? AND household_id = ?", (feedback_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "feedback_updated"}, household_id=household_id)
        return {"message": "Feedback deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_feedback: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/feedback/{feedback_id}/vote")
async def toggle_vote(feedback_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Toggle upvote on a feedback item (vote / unvote)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            # Verify feedback exists
            existing = conn.execute(
                "SELECT id FROM feedback WHERE id = ? AND household_id = ?",
                (feedback_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Feedback not found")

            # Check if already voted
            vote = conn.execute(
                "SELECT id FROM feedback_votes WHERE feedback_id = ? AND voter = ? AND household_id = ?",
                (feedback_id, tenant.display_name, household_id)
            ).fetchone()

            if vote:
                # Unvote
                conn.execute(
                    "DELETE FROM feedback_votes WHERE feedback_id = ? AND voter = ? AND household_id = ?",
                    (feedback_id, tenant.display_name, household_id)
                )
                voted = False
            else:
                # Vote
                conn.execute(
                    "INSERT INTO feedback_votes (feedback_id, voter, household_id) VALUES (?, ?, ?)",
                    (feedback_id, tenant.display_name, household_id)
                )
                voted = True
            conn.commit()

            # Get updated vote count
            count = conn.execute(
                "SELECT COUNT(*) as c FROM feedback_votes WHERE feedback_id = ?",
                (feedback_id,)
            ).fetchone()["c"]

        await manager.broadcast({"type": "feedback_updated"}, household_id=household_id)
        return {"voted": voted, "vote_count": count}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in toggle_vote: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in toggle_vote: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# FEEDBACK SCREENSHOTS
# ============================================================================


@router.post("/api/feedback/{feedback_id}/screenshot")
async def upload_screenshot(feedback_id: int, file: UploadFile = File(...), tenant: TenantContext = Depends(get_current_user)):
    """Upload a screenshot for a feedback item. Overwrites any existing screenshot."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM feedback WHERE id = ? AND household_id = ?",
                (feedback_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Feedback not found")

            is_owner = existing["submitted_by"] == tenant.display_name
            is_manager = tenant.role == "manager"
            if not is_owner and not is_manager:
                raise HTTPException(status_code=403, detail="Not authorized")

        data = await file.read()
        if len(data) > SCREENSHOT_MAX_SIZE:
            raise HTTPException(status_code=400, detail="File too large (max 10MB)")

        # Save as JPEG, resized
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            # Resize if very large
            max_dim = 1920
            if img.width > max_dim or img.height > max_dim:
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            save_path = FEEDBACK_SCREENSHOT_DIR / f"{feedback_id}.jpg"
            img.save(save_path, "JPEG", quality=85)
        except Exception as e:
            logger.error("Failed to process screenshot: %s", e)
            raise HTTPException(status_code=400, detail="Invalid image file")

        # Update DB
        rel_path = f"static/feedback_screenshots/{feedback_id}.jpg"
        with get_db() as conn:
            conn.execute(
                "UPDATE feedback SET screenshot_path = ? WHERE id = ? AND household_id = ?",
                (rel_path, feedback_id, household_id)
            )
            conn.commit()

        return {"screenshot_url": f"/{rel_path}?t={int(time.time())}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Error uploading screenshot: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/feedback/{feedback_id}/screenshot")
def delete_screenshot(feedback_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a feedback screenshot."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM feedback WHERE id = ? AND household_id = ?",
                (feedback_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Feedback not found")

            is_owner = existing["submitted_by"] == tenant.display_name
            is_manager = tenant.role == "manager"
            if not is_owner and not is_manager:
                raise HTTPException(status_code=403, detail="Not authorized")

            # Delete file
            screenshot_file = FEEDBACK_SCREENSHOT_DIR / f"{feedback_id}.jpg"
            if screenshot_file.exists():
                screenshot_file.unlink()

            conn.execute(
                "UPDATE feedback SET screenshot_path = NULL WHERE id = ? AND household_id = ?",
                (feedback_id, household_id)
            )
            conn.commit()

        return {"message": "Screenshot deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Error deleting screenshot: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# FEEDBACK REPLIES
# ============================================================================

@router.get("/api/feedback/{feedback_id}/replies")
def list_replies(feedback_id: int, tenant: TenantContext = Depends(get_current_user)):
    """List replies on a feedback item."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            is_superadmin = _is_superadmin(conn, tenant.user_id)

            # Verify feedback exists and belongs to household (or superadmin)
            if is_superadmin:
                fb = conn.execute("SELECT id FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
            else:
                fb = conn.execute(
                    "SELECT id FROM feedback WHERE id = ? AND household_id = ?",
                    (feedback_id, household_id)
                ).fetchone()
            if not fb:
                raise HTTPException(status_code=404, detail="Feedback not found")

            rows = conn.execute(
                "SELECT * FROM feedback_replies WHERE feedback_id = ? ORDER BY created_at ASC",
                (feedback_id,)
            ).fetchall()
            return {"replies": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error listing replies: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/feedback/{feedback_id}/reply")
async def add_reply(feedback_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Add a reply to a feedback item. Managers/superadmin replies are marked as admin."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        reply_text = data.get("text", "").strip() if isinstance(data.get("text"), str) else ""
        if not reply_text:
            raise HTTPException(status_code=400, detail="Reply text is required")
        if len(reply_text) > 2000:
            raise HTTPException(status_code=400, detail="Reply too long (max 2000 characters)")

        with get_db() as conn:
            is_superadmin = _is_superadmin(conn, tenant.user_id)
            is_admin = tenant.role == "manager" or is_superadmin

            # Verify feedback exists
            if is_superadmin:
                fb = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
            else:
                fb = conn.execute(
                    "SELECT * FROM feedback WHERE id = ? AND household_id = ?",
                    (feedback_id, household_id)
                ).fetchone()
            if not fb:
                raise HTTPException(status_code=404, detail="Feedback not found")

            feedback_household_id = fb["household_id"]

            cursor = conn.execute(
                """INSERT INTO feedback_replies (feedback_id, reply_by, reply_text, is_admin, household_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (feedback_id, tenant.display_name, reply_text, 1 if is_admin else 0, feedback_household_id)
            )
            conn.commit()
            reply_id = cursor.lastrowid

            # Auto-acknowledge feedback on first admin reply if still 'new'
            if is_admin and fb["status"] == "new":
                conn.execute(
                    "UPDATE feedback SET status = 'acknowledged', updated_at = datetime('now') WHERE id = ?",
                    (feedback_id,)
                )
                conn.commit()

        await manager.broadcast({"type": "feedback_updated"}, household_id=feedback_household_id)

        # Notify the original poster if admin is replying
        if is_admin and fb["submitted_by"] != tenant.display_name:
            try:
                from push import send_push_to_person_bg
                send_push_to_person_bg(
                    fb["submitted_by"],
                    "Huddle: Reply to your feedback",
                    f"Admin replied to '{fb['title']}': {reply_text[:100]}",
                    tag="feedback",
                    household_id=feedback_household_id,
                )
            except Exception as e:
                logger.warning("Failed to send reply push: %s", e)

        return {"id": reply_id, "message": "Reply added"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error adding reply: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


def _send_status_email_to_poster(feedback_id: int, title: str, admin_notes: str, submitted_by: str, household_id: int):
    """Email the original feedback poster when their item is resolved."""
    try:
        import resend
        from config import RESEND_API_KEY

        if not RESEND_API_KEY:
            return

        resend.api_key = RESEND_API_KEY

        # Look up the poster's email
        with get_db() as conn:
            row = conn.execute("""
                SELECT u.email FROM users u
                JOIN household_members hm ON u.id = hm.user_id
                WHERE hm.display_name = ? AND hm.household_id = ?
            """, (submitted_by, household_id)).fetchone()
            if not row or "@placeholder" in row["email"]:
                return
            poster_email = row["email"]

        notes_html = f'<p style="margin:16px 0 0;color:#e0e0e0;font-size:14px;line-height:1.5;"><strong style="color:#999;">Notes:</strong> {admin_notes}</p>' if admin_notes else ""

        html = f"""
        <div style="background-color:#1a1a2e;padding:0;margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
            <div style="max-width:560px;margin:0 auto;padding:32px 16px;">
                <div style="text-align:center;margin-bottom:28px;">
                    <div style="display:inline-block;background:linear-gradient(135deg,#4ecdc4,#44a08d);width:48px;height:48px;border-radius:14px;line-height:48px;font-size:22px;margin-bottom:12px;">&#127968;</div>
                    <h1 style="margin:0;color:#ffffff;font-size:20px;font-weight:700;">Huddle</h1>
                </div>
                <div style="text-align:center;margin-bottom:20px;">
                    <span style="display:inline-block;background:#4caf50;color:#fff;font-size:12px;font-weight:700;padding:5px 14px;border-radius:20px;text-transform:uppercase;">
                        &#10004; Resolved
                    </span>
                </div>
                <div style="background-color:#16213e;border-radius:16px;border:1px solid #2a2a4a;padding:24px;">
                    <h2 style="margin:0 0 8px;color:#ffffff;font-size:18px;">Your feedback has been resolved</h2>
                    <p style="margin:0;color:#e0e0e0;font-size:14px;">"{title}"</p>
                    {notes_html}
                </div>
                <div style="text-align:center;margin-top:20px;">
                    <a href="https://huddle.rodgersgroup.au/mobile?module=feedback"
                       style="display:inline-block;background:linear-gradient(135deg,#4ecdc4,#44a08d);color:#fff;text-decoration:none;font-size:14px;font-weight:600;padding:12px 32px;border-radius:10px;">
                        View in Huddle &rarr;
                    </a>
                </div>
                <p style="text-align:center;color:#555;font-size:12px;margin-top:24px;">Huddle &middot; Household Management</p>
            </div>
        </div>
        """

        resend.Emails.send({
            "from": "Huddle <noreply@huddle.rodgersgroup.au>",
            "to": [poster_email],
            "subject": f"[Huddle] Your feedback has been resolved: {title}",
            "html": html,
        })
        logger.info("Feedback resolved email sent to %s for: %s", poster_email, title)
    except Exception as e:
        logger.warning("Failed to send feedback resolved email: %s", e)
