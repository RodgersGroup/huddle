import logging
import sqlite3
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext
from datetime import datetime
from db import get_db
from settings import get_people
from websocket import manager
from push import send_push_to_person_bg

logger = logging.getLogger("huddle")

router = APIRouter()

VALID_CATEGORIES = {"general", "noise", "guests", "cleaning", "kitchen", "bathroom", "parking", "pets", "other"}

STARTER_TEMPLATES = [
    {"title": "Quiet Hours", "description": "Please keep noise to a minimum between 10:00 PM and 7:00 AM on weeknights, and 11:00 PM to 8:00 AM on weekends.", "category": "noise"},
    {"title": "Overnight Guests", "description": "Let housemates know at least 24 hours in advance if you're having guests stay overnight.", "category": "guests"},
    {"title": "Kitchen Cleanliness", "description": "Wash your dishes within 2 hours of use. Don't leave food in the fridge past its use-by date.", "category": "kitchen"},
    {"title": "Bathroom Etiquette", "description": "Clean up after yourself. Hair out of drains. Replace the toilet roll when it runs out.", "category": "bathroom"},
    {"title": "Shared Spaces", "description": "Keep common areas tidy. Personal items should be stored in your room.", "category": "general"},
]


@router.get("/api/house-rules/templates")
def get_templates(tenant: TenantContext = Depends(get_current_user)):
    """Return starter templates for empty house rules lists."""
    return {"templates": STARTER_TEMPLATES}


@router.put("/api/house-rules/reorder")
async def reorder_rules(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Reorder house rules. Manager only."""
    household_id = tenant.household_id
    if tenant.role != "manager":
        raise HTTPException(status_code=403, detail="Manager only")
    try:
        data = await request.json()

        if not isinstance(data, list):
            logger.warning("Validation failed in reorder_rules: expected array of {id, sort_order}")
            raise HTTPException(status_code=400, detail="Expected array of {id, sort_order}")

        with get_db() as conn:
            for item in data:
                rule_id = item.get("id")
                sort_order = item.get("sort_order")
                if rule_id is None or sort_order is None:
                    logger.warning("Validation failed in reorder_rules: each item must have id and sort_order")
                    raise HTTPException(status_code=400, detail="Each item must have id and sort_order")
                if not isinstance(sort_order, int):
                    logger.warning("Validation failed in reorder_rules: sort_order must be an integer")
                    raise HTTPException(status_code=400, detail="sort_order must be an integer")
                conn.execute(
                    "UPDATE house_rules SET sort_order = ? WHERE id = ? AND household_id = ?",
                    (sort_order, rule_id, household_id)
                )
            conn.commit()

        await manager.broadcast({"type": "house_rules_updated"}, household_id=household_id)
        return {"message": "Rules reordered"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in reorder_rules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in reorder_rules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/house-rules")
def list_rules(tenant: TenantContext = Depends(get_current_user)):
    """List all house rules with acknowledgement status."""
    household_id = tenant.household_id
    try:
        people = get_people(household_id=household_id)

        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM house_rules
                WHERE household_id = ? AND deleted_at IS NULL
                ORDER BY sort_order ASC, created_at ASC
            """, (household_id,)).fetchall()

            rules = []
            for row in rows:
                rule = dict(row)
                acks = conn.execute(
                    "SELECT person FROM house_rule_acknowledgements WHERE rule_id = ? AND household_id = ?",
                    (rule["id"], household_id)
                ).fetchall()
                acknowledged_by = [a["person"] for a in acks]
                not_acknowledged = [p for p in people if p not in acknowledged_by]
                rule["acknowledged_by"] = acknowledged_by
                rule["not_acknowledged"] = not_acknowledged
                rules.append(rule)

            return {"rules": rules}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in list_rules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in list_rules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/house-rules")
async def create_rule(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Create a house rule."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        title = data.get("title", "").strip() if isinstance(data.get("title"), str) else ""
        if not title:
            logger.warning("Validation failed in create_rule: title required")
            raise HTTPException(status_code=400, detail="Title is required")
        if len(title) > 200:
            logger.warning("Validation failed in create_rule: title too long")
            raise HTTPException(status_code=400, detail="Title too long (max 200)")

        description = data.get("description", "").strip() if isinstance(data.get("description"), str) else ""
        if len(description) > 2000:
            logger.warning("Validation failed in create_rule: description too long")
            raise HTTPException(status_code=400, detail="Description too long (max 2000)")

        category = data.get("category", "general")
        if category not in VALID_CATEGORIES:
            category = "general"

        now = datetime.now().isoformat()

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO house_rules (title, description, category, sort_order, created_by, created_at, updated_at, household_id)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?)
            """, (title, description, category, tenant.display_name, now, now, household_id))
            conn.commit()
            rule_id = cursor.lastrowid

        await manager.broadcast({"type": "house_rules_updated"}, household_id=household_id)

        # Push notification to all members except creator
        try:
            for person in get_people(household_id=household_id):
                if person != tenant.display_name:
                    send_push_to_person_bg(person, "Huddle: New House Rule", f"{title}", "house-rule", household_id=household_id)
        except Exception as push_err:
            logger.warning("House rule push failed: %s", push_err)

        return {"id": rule_id, "message": "Rule created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/house-rules/{rule_id}")
async def update_rule(rule_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update a house rule. Manager only. Clears all acknowledgements so everyone must re-acknowledge."""
    household_id = tenant.household_id
    if tenant.role != "manager":
        raise HTTPException(status_code=403, detail="Manager only")
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM house_rules WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (rule_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Rule not found")

            title = data.get("title", existing["title"])
            if isinstance(title, str):
                title = title.strip()
            if not title:
                logger.warning("Validation failed in update_rule: title required")
                raise HTTPException(status_code=400, detail="Title is required")
            if len(title) > 200:
                logger.warning("Validation failed in update_rule: title too long")
                raise HTTPException(status_code=400, detail="Title too long (max 200)")

            description = data.get("description", existing["description"])
            if isinstance(description, str):
                description = description.strip()
            if description and len(description) > 2000:
                logger.warning("Validation failed in update_rule: description too long")
                raise HTTPException(status_code=400, detail="Description too long (max 2000)")

            category = data.get("category", existing["category"])
            if category not in VALID_CATEGORIES:
                category = existing["category"]

            now = datetime.now().isoformat()

            conn.execute("""
                UPDATE house_rules
                SET title = ?, description = ?, category = ?, updated_at = ?
                WHERE id = ? AND household_id = ?
            """, (title, description, category, now, rule_id, household_id))

            # Clear all acknowledgements — everyone must re-acknowledge
            conn.execute(
                "DELETE FROM house_rule_acknowledgements WHERE rule_id = ? AND household_id = ?",
                (rule_id, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "house_rules_updated"}, household_id=household_id)

        # Push notification to all members
        try:
            for person in get_people(household_id=household_id):
                send_push_to_person_bg(
                    person,
                    "Huddle: House Rule Updated",
                    f"House rule updated: {title} \u2014 please review and acknowledge",
                    "house-rule",
                    household_id=household_id,
                )
        except Exception as push_err:
            logger.warning("House rule update push failed: %s", push_err)

        return {"message": "Rule updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/house-rules/{rule_id}")
async def delete_rule(rule_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a house rule. Manager only."""
    household_id = tenant.household_id
    if tenant.role != "manager":
        raise HTTPException(status_code=403, detail="Manager only")
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM house_rules WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (rule_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Rule not found")

            conn.execute("UPDATE house_rules SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?", (rule_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "house_rules_updated"}, household_id=household_id)
        return {"message": "Rule deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/house-rules/{rule_id}/acknowledge")
async def acknowledge_rule(rule_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Acknowledge a house rule as the current user."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM house_rules WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (rule_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Rule not found")

            conn.execute("""
                INSERT OR IGNORE INTO house_rule_acknowledgements (rule_id, person, acknowledged_at, household_id)
                VALUES (?, ?, ?, ?)
            """, (rule_id, tenant.display_name, datetime.now().isoformat(), household_id))
            conn.commit()

        await manager.broadcast({"type": "house_rules_updated"}, household_id=household_id)
        return {"message": "Rule acknowledged"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in acknowledge_rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in acknowledge_rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
