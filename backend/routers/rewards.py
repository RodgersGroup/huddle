"""Rewards & Stars API — points for chores/routines, reward shop, redemptions."""
import logging
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, require_role, TenantContext
from db import get_db, check_version
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()


@router.get("/api/rewards/points")
def get_points(tenant: TenantContext = Depends(get_current_user)):
    """Get star balances per person."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT person, SUM(points) as total FROM reward_points WHERE household_id = ? GROUP BY person",
                (household_id,)
            ).fetchall()
            balances = {r["person"]: r["total"] for r in rows}

        return {"balances": balances}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_points: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_points: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/rewards/points/{person}")
def get_person_points(person: str, tenant: TenantContext = Depends(get_current_user)):
    """Get point history for a specific person."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            total = conn.execute(
                "SELECT COALESCE(SUM(points), 0) as total FROM reward_points WHERE person = ? AND household_id = ?",
                (person, household_id)
            ).fetchone()["total"]

            history = conn.execute(
                "SELECT points, reason, source_type, awarded_by, created_at FROM reward_points WHERE person = ? AND household_id = ? ORDER BY created_at DESC LIMIT 50",
                (person, household_id)
            ).fetchall()

            return {
                "person": person,
                "total": total,
                "history": [dict(r) for r in history],
            }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_person_points: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_person_points: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/rewards/points")
async def award_points(request: Request, tenant: TenantContext = Depends(require_role("manager"))):
    """Manually award or deduct stars."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        person = data.get("person", "").strip()
        if not person:
            raise HTTPException(status_code=400, detail="person is required")
        points = data.get("points", 0)
        if not isinstance(points, int) or points == 0:
            raise HTTPException(status_code=400, detail="points must be a non-zero integer")
        reason = data.get("reason", "").strip() or ("Manual award" if points > 0 else "Manual deduction")

        with get_db() as conn:
            conn.execute(
                "INSERT INTO reward_points (person, points, reason, source_type, awarded_by, household_id) VALUES (?, ?, ?, ?, ?, ?)",
                (person, points, reason, "manual", tenant.display_name, household_id)
            )
            conn.commit()

            new_total = conn.execute(
                "SELECT COALESCE(SUM(points), 0) as total FROM reward_points WHERE person = ? AND household_id = ?",
                (person, household_id)
            ).fetchone()["total"]

        await manager.broadcast({"type": "rewards_updated"}, household_id=household_id)
        return {"message": "Points awarded", "person": person, "new_total": new_total}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in award_points: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in award_points: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/rewards/shop")
def get_rewards_shop(tenant: TenantContext = Depends(get_current_user)):
    """Get available rewards in the shop."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rewards = conn.execute(
                "SELECT * FROM rewards WHERE household_id = ? AND available = 1 AND deleted_at IS NULL ORDER BY cost, name",
                (household_id,)
            ).fetchall()

        return {"rewards": [dict(r) for r in rewards]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_rewards_shop: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_rewards_shop: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/rewards/shop")
async def create_reward(request: Request, tenant: TenantContext = Depends(require_role("manager"))):
    """Create a new reward in the shop."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        name = data.get("name", "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Reward name is required")
        if len(name) > 200:
            raise HTTPException(status_code=400, detail="Name too long (max 200)")
        cost = data.get("cost", 0)
        if not isinstance(cost, int) or cost < 1:
            raise HTTPException(status_code=400, detail="cost must be a positive integer")
        description = data.get("description", "").strip()
        icon = data.get("icon", "🎁").strip()

        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO rewards (name, description, cost, icon, household_id) VALUES (?, ?, ?, ?, ?)",
                (name, description, cost, icon, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "rewards_updated"}, household_id=household_id)
        return {"id": cursor.lastrowid, "message": "Reward created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_reward: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_reward: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/rewards/shop/{reward_id}")
async def update_reward(reward_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager"))):
    """Update a reward."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM rewards WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (reward_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Reward not found")
            check_version(data, existing, "reward")

            name = data.get("name", existing["name"]).strip()
            cost = data.get("cost", existing["cost"])
            description = data.get("description", existing["description"] or "")
            icon = data.get("icon", existing["icon"])
            available = data.get("available", existing["available"])

            conn.execute(
                "UPDATE rewards SET name = ?, description = ?, cost = ?, icon = ?, available = ?, version = COALESCE(version, 0) + 1, updated_at = datetime('now') WHERE id = ? AND household_id = ?",
                (name, description, cost, icon, 1 if available else 0, reward_id, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "rewards_updated"}, household_id=household_id)
        return {"message": "Reward updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_reward: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_reward: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/rewards/shop/{reward_id}")
async def delete_reward(reward_id: int, tenant: TenantContext = Depends(require_role("manager"))):
    """Delete a reward from the shop."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM rewards WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (reward_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Reward not found")

            conn.execute("UPDATE rewards SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?", (reward_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "rewards_updated"}, household_id=household_id)
        return {"message": "Reward deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_reward: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_reward: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/rewards/redeem/{reward_id}")
async def redeem_reward(reward_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Redeem a reward (creates pending redemption, deducts points)."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        person = data.get("person", "").strip()
        if not person:
            raise HTTPException(status_code=400, detail="person is required")

        with get_db() as conn:
            reward = conn.execute(
                "SELECT * FROM rewards WHERE id = ? AND household_id = ? AND available = 1 AND deleted_at IS NULL",
                (reward_id, household_id)
            ).fetchone()
            if not reward:
                raise HTTPException(status_code=404, detail="Reward not found or unavailable")

            # Check balance
            balance = conn.execute(
                "SELECT COALESCE(SUM(points), 0) as total FROM reward_points WHERE person = ? AND household_id = ?",
                (person, household_id)
            ).fetchone()["total"]

            if balance < reward["cost"]:
                raise HTTPException(status_code=400, detail=f"Not enough stars ({balance}/{reward['cost']})")

            # Deduct points
            conn.execute(
                "INSERT INTO reward_points (person, points, reason, source_type, source_id, household_id) VALUES (?, ?, ?, ?, ?, ?)",
                (person, -reward["cost"], f"Redeemed: {reward['name']}", "redemption", reward_id, household_id)
            )

            # Create redemption record
            conn.execute(
                "INSERT INTO reward_redemptions (reward_id, person, points_spent, status, household_id) VALUES (?, ?, ?, ?, ?)",
                (reward_id, person, reward["cost"], "pending", household_id)
            )
            conn.commit()

            new_balance = conn.execute(
                "SELECT COALESCE(SUM(points), 0) as total FROM reward_points WHERE person = ? AND household_id = ?",
                (person, household_id)
            ).fetchone()["total"]

        # Notify managers
        try:
            from push import send_push_to_person_bg, PUSH_ENABLED
            from settings import get_people
            if PUSH_ENABLED:
                people = get_people(household_id=household_id)
                for p in people:
                    if p != person:
                        send_push_to_person_bg(
                            p,
                            f"Huddle: Reward redeemed!",
                            f"{person} redeemed {reward['name']} ({reward['cost']} stars)",
                            "reward-redeemed",
                            household_id=household_id,
                        )
        except Exception as e:
            logger.warning("Push notification failed for reward redemption: %s", e)

        await manager.broadcast({"type": "rewards_updated"}, household_id=household_id)
        return {"message": "Reward redeemed!", "new_balance": new_balance}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in redeem_reward: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in redeem_reward: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/rewards/redemptions")
def get_redemptions(tenant: TenantContext = Depends(get_current_user)):
    """Get recent redemptions."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT rr.*, r.name as reward_name, r.icon as reward_icon
                   FROM reward_redemptions rr
                   JOIN rewards r ON rr.reward_id = r.id
                   WHERE rr.household_id = ?
                   ORDER BY rr.created_at DESC LIMIT 50""",
                (household_id,)
            ).fetchall()

        return {"redemptions": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_redemptions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_redemptions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/rewards/redemptions/{redemption_id}/approve")
async def approve_redemption(redemption_id: int, tenant: TenantContext = Depends(require_role("manager"))):
    """Approve a pending redemption (manager only)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            redemption = conn.execute(
                "SELECT * FROM reward_redemptions WHERE id = ? AND household_id = ? AND status = 'pending'",
                (redemption_id, household_id)
            ).fetchone()
            if not redemption:
                raise HTTPException(status_code=404, detail="Redemption not found or already processed")

            conn.execute(
                "UPDATE reward_redemptions SET status = 'approved', approved_by = ?, approved_at = datetime('now') WHERE id = ? AND household_id = ?",
                (tenant.display_name, redemption_id, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "rewards_updated"}, household_id=household_id)
        return {"message": "Redemption approved"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in approve_redemption: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in approve_redemption: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
