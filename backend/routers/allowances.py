"""Allowances API — pocket money tracking with savings goals."""
import logging
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, require_role, TenantContext
from db import get_db
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()


@router.get("/api/allowances")
def get_allowances(tenant: TenantContext = Depends(get_current_user)):
    """Get allowance settings, balances, and savings goals for all people."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            # Allowance settings
            settings = conn.execute(
                "SELECT * FROM allowance_settings WHERE household_id = ? AND active = 1 ORDER BY person",
                (household_id,)
            ).fetchall()

            # Calculate balances per person
            balances = {}
            for s in settings:
                total = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) as total FROM allowance_transactions WHERE person = ? AND household_id = ?",
                    (s["person"], household_id)
                ).fetchone()["total"]
                balances[s["person"]] = round(total, 2)

            # Savings goals
            goals = conn.execute(
                "SELECT * FROM savings_goals WHERE household_id = ? AND completed = 0 ORDER BY person, name",
                (household_id,)
            ).fetchall()

            return {
                "settings": [dict(s) for s in settings],
                "balances": balances,
                "goals": [dict(g) for g in goals],
            }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_allowances: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_allowances: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/allowances/settings")
async def create_allowance_setting(request: Request, tenant: TenantContext = Depends(require_role("manager"))):
    """Create or update allowance settings for a person."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        person = data.get("person", "").strip()
        if not person:
            raise HTTPException(status_code=400, detail="person is required")
        amount = data.get("amount", 5.0)
        if not isinstance(amount, (int, float)) or amount < 0:
            raise HTTPException(status_code=400, detail="amount must be non-negative")
        frequency = data.get("frequency", "weekly")
        if frequency not in ("weekly", "fortnightly", "monthly"):
            raise HTTPException(status_code=400, detail="Invalid frequency")
        pay_day = data.get("pay_day", 0)

        with get_db() as conn:
            conn.execute("""
                INSERT INTO allowance_settings (person, amount, frequency, pay_day, household_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(person, household_id) DO UPDATE SET amount = ?, frequency = ?, pay_day = ?, active = 1
            """, (person, amount, frequency, pay_day, household_id, amount, frequency, pay_day))
            conn.commit()

        await manager.broadcast({"type": "allowances_updated"}, household_id=household_id)
        return {"message": "Allowance settings saved"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_allowance_setting: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_allowance_setting: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/allowances/transaction")
async def create_transaction(request: Request, tenant: TenantContext = Depends(require_role("manager"))):
    """Record an allowance transaction (credit/debit/purchase)."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        person = data.get("person", "").strip()
        if not person:
            raise HTTPException(status_code=400, detail="person is required")
        amount = data.get("amount", 0)
        if not isinstance(amount, (int, float)) or amount == 0:
            raise HTTPException(status_code=400, detail="amount must be non-zero")
        transaction_type = data.get("transaction_type", "manual")
        if transaction_type not in ("allowance", "purchase", "manual", "bonus"):
            raise HTTPException(status_code=400, detail="Invalid transaction_type")
        description = data.get("description", "").strip()

        with get_db() as conn:
            conn.execute(
                "INSERT INTO allowance_transactions (person, amount, transaction_type, description, approved_by, household_id) VALUES (?, ?, ?, ?, ?, ?)",
                (person, amount, transaction_type, description, tenant.display_name, household_id)
            )
            conn.commit()

            new_balance = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM allowance_transactions WHERE person = ? AND household_id = ?",
                (person, household_id)
            ).fetchone()["total"]

        await manager.broadcast({"type": "allowances_updated"}, household_id=household_id)
        return {"message": "Transaction recorded", "new_balance": round(new_balance, 2)}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_transaction: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_transaction: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/allowances/transactions/{person}")
def get_transactions(person: str, tenant: TenantContext = Depends(get_current_user)):
    """Get transaction history for a person."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM allowance_transactions WHERE person = ? AND household_id = ? ORDER BY created_at DESC LIMIT 50",
                (person, household_id)
            ).fetchall()

        return {"transactions": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_transactions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_transactions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/allowances/goals")
async def create_savings_goal(request: Request, tenant: TenantContext = Depends(require_role("manager"))):
    """Create a savings goal."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        person = data.get("person", "").strip()
        if not person:
            raise HTTPException(status_code=400, detail="person is required")
        name = data.get("name", "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Goal name is required")
        target_amount = data.get("target_amount", 0)
        if not isinstance(target_amount, (int, float)) or target_amount <= 0:
            raise HTTPException(status_code=400, detail="target_amount must be positive")
        icon = data.get("icon", "🎯")

        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO savings_goals (person, name, target_amount, icon, household_id) VALUES (?, ?, ?, ?, ?)",
                (person, name, target_amount, icon, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "allowances_updated"}, household_id=household_id)
        return {"id": cursor.lastrowid, "message": "Goal created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_savings_goal: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_savings_goal: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/allowances/goals/{goal_id}/contribute")
async def contribute_to_goal(goal_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager"))):
    """Add money to a savings goal from allowance balance."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        amount = data.get("amount", 0)
        if not isinstance(amount, (int, float)) or amount <= 0:
            raise HTTPException(status_code=400, detail="amount must be positive")

        with get_db() as conn:
            goal = conn.execute(
                "SELECT * FROM savings_goals WHERE id = ? AND household_id = ? AND completed = 0",
                (goal_id, household_id)
            ).fetchone()
            if not goal:
                raise HTTPException(status_code=404, detail="Goal not found")

            # Check balance
            balance = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM allowance_transactions WHERE person = ? AND household_id = ?",
                (goal["person"], household_id)
            ).fetchone()["total"]

            if balance < amount:
                raise HTTPException(status_code=400, detail=f"Insufficient balance (${balance:.2f})")

            # Deduct from balance
            conn.execute(
                "INSERT INTO allowance_transactions (person, amount, transaction_type, description, household_id) VALUES (?, ?, ?, ?, ?)",
                (goal["person"], -amount, "savings", f"Saved for: {goal['name']}", household_id)
            )

            # Add to goal
            new_amount = goal["current_amount"] + amount
            remaining = goal["target_amount"] - new_amount
            completed = 1 if new_amount >= goal["target_amount"] else 0

            conn.execute(
                "UPDATE savings_goals SET current_amount = ?, completed = ?, completed_at = CASE WHEN ? = 1 THEN datetime('now') ELSE NULL END WHERE id = ? AND household_id = ?",
                (new_amount, completed, completed, goal_id, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "allowances_updated"}, household_id=household_id)
        result = {"message": "Saved!", "new_amount": round(new_amount, 2), "completed": completed == 1}
        if completed:
            result["message"] = f"🎉 Goal reached! {goal['name']}"
        return result
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in contribute_to_goal: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in contribute_to_goal: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/allowances/goals/{goal_id}")
async def delete_savings_goal(goal_id: int, tenant: TenantContext = Depends(require_role("manager"))):
    """Delete a savings goal."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            goal = conn.execute(
                "SELECT id FROM savings_goals WHERE id = ? AND household_id = ?",
                (goal_id, household_id)
            ).fetchone()
            if not goal:
                raise HTTPException(status_code=404, detail="Goal not found")

            conn.execute("DELETE FROM savings_goals WHERE id = ? AND household_id = ?", (goal_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "allowances_updated"}, household_id=household_id)
        return {"message": "Goal deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_savings_goal: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_savings_goal: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
