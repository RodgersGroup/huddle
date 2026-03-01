import json
import logging
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from auth import get_current_user, require_role, TenantContext
from collections import defaultdict
from datetime import datetime, date
from zoneinfo import ZoneInfo
from db import get_db, check_version
from settings import get_setting, get_people
from websocket import manager
from push import send_push_to_person_bg

logger = logging.getLogger("huddle")

router = APIRouter()


@router.get("/api/bills")
def get_bills(tenant: TenantContext = Depends(get_current_user), since: str | None = Query(None)):
    """Get all bills, flagging overdue ones. Pass ?since=<timestamp> for delta sync."""
    household_id = tenant.household_id
    try:
        tz = ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))
        today = datetime.now(tz).date().isoformat()
        with get_db() as conn:
            if since:
                rows = conn.execute(
                    "SELECT * FROM bills WHERE household_id = ? AND deleted_at IS NULL AND updated_at > ? ORDER BY paid ASC, due_date ASC",
                    (household_id, since),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bills WHERE household_id = ? AND deleted_at IS NULL ORDER BY paid ASC, due_date ASC",
                    (household_id,),
                ).fetchall()
            bills = []
            for row in rows:
                bill = dict(row)
                bill['overdue'] = bill['paid'] == 0 and bill['due_date'] < today
                bills.append(bill)
            response = {"bills": bills}
            if since:
                deleted_rows = conn.execute(
                    "SELECT id FROM bills WHERE household_id = ? AND deleted_at > ?",
                    (household_id, since),
                ).fetchall()
                response["deleted"] = [r["id"] for r in deleted_rows]
            return response
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_bills: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_bills: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/bills")
async def create_bill(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Create a new bill."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
        if not name:
            logger.warning("Validation failed in create_bill: %s", "Name is required")
            raise HTTPException(status_code=400, detail="Name is required")
        if len(name) > 200:
            logger.warning("Validation failed in create_bill: %s", "Name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")
        data['name'] = name

        amount = data.get('amount')
        if amount is not None:
            try:
                amount = float(amount)
                if amount < 0 or amount > 999999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in create_bill: %s", "Amount must be a valid number (0 to 999999999)")
                raise HTTPException(status_code=400, detail="Amount must be a valid number (0 to 999999999)")
            data['amount'] = amount

        if not data.get('due_date'):
            logger.warning("Validation failed in create_bill: %s", "Due date is required")
            raise HTTPException(status_code=400, detail="Due date is required")

        notes = data.get('notes', '')
        if isinstance(notes, str) and len(notes) > 1000:
            logger.warning("Validation failed in create_bill: %s", "Notes are too long (max 1000 characters)")
            raise HTTPException(status_code=400, detail="Notes are too long (max 1000 characters)")
        # --- End validation ---

        split_type = data.get('split_type', 'equal')
        if split_type not in ('equal', 'custom', 'single'):
            split_type = 'equal'
        split_between = data.get('split_between', [])
        if not isinstance(split_between, list):
            split_between = []
        split_between_json = json.dumps(split_between)

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO bills (name, amount, due_date, recurrence, category, notes, split_type, split_between, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['name'],
                data.get('amount'),
                data['due_date'],
                data.get('recurrence', 'once'),
                data.get('category', 'other'),
                data.get('notes', ''),
                split_type,
                split_between_json,
                household_id
            ))
            conn.commit()
            bill_id = cursor.lastrowid
        await manager.broadcast({"type": "bills_updated"}, household_id=household_id)
        return {"id": bill_id, "message": "Bill created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/bills/{bill_id}")
async def update_bill(bill_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Update a bill."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        if 'name' in data:
            name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
            if not name:
                logger.warning("Validation failed in update_bill: %s", "Name cannot be empty")
                raise HTTPException(status_code=400, detail="Name cannot be empty")
            if len(name) > 200:
                logger.warning("Validation failed in update_bill: %s", "Name is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")
            data['name'] = name

        if 'amount' in data and data['amount'] is not None:
            try:
                amount = float(data['amount'])
                if amount < 0 or amount > 999999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in update_bill: %s", "Amount must be a valid number (0 to 999999999)")
                raise HTTPException(status_code=400, detail="Amount must be a valid number (0 to 999999999)")
            data['amount'] = amount

        if 'notes' in data:
            notes = data.get('notes', '')
            if isinstance(notes, str) and len(notes) > 1000:
                logger.warning("Validation failed in update_bill: %s", "Notes are too long (max 1000 characters)")
                raise HTTPException(status_code=400, detail="Notes are too long (max 1000 characters)")
        # --- End validation ---

        with get_db() as conn:
            existing = conn.execute("SELECT * FROM bills WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (bill_id, household_id)).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Bill not found")
            check_version(data, existing, "bill")
            split_type = data.get('split_type', existing['split_type'] or 'equal')
            if split_type not in ('equal', 'custom', 'single'):
                split_type = 'equal'
            split_between = data.get('split_between')
            if split_between is not None:
                if not isinstance(split_between, list):
                    split_between = []
                split_between_json = json.dumps(split_between)
            else:
                split_between_json = existing['split_between'] or '[]'

            conn.execute("""
                UPDATE bills SET name=?, amount=?, due_date=?, recurrence=?, category=?, notes=?, split_type=?, split_between=?,
                    version = COALESCE(version, 0) + 1, updated_at = datetime('now')
                WHERE id=? AND household_id=?
            """, (
                data.get('name', existing['name']),
                data.get('amount', existing['amount']),
                data.get('due_date', existing['due_date']),
                data.get('recurrence', existing['recurrence']),
                data.get('category', existing['category']),
                data.get('notes', existing['notes']),
                split_type,
                split_between_json,
                bill_id,
                household_id
            ))
            conn.commit()
        await manager.broadcast({"type": "bills_updated"}, household_id=household_id)
        return {"message": "Bill updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/bills/{bill_id}")
async def delete_bill(bill_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Delete a bill."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute("UPDATE bills SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?", (bill_id, household_id))
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Bill not found")
        await manager.broadcast({"type": "bills_updated"}, household_id=household_id)
        return {"message": "Bill deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/bills/{bill_id}/pay")
async def pay_bill(bill_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Mark a bill as paid. If recurring, auto-create next occurrence."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        paid_by = data.get('paid_by')
        with get_db() as conn:
            bill = conn.execute("SELECT * FROM bills WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (bill_id, household_id)).fetchone()
            if not bill:
                raise HTTPException(status_code=404, detail="Bill not found")
            conn.execute("""
                UPDATE bills SET paid=1, paid_by=?, paid_at=? WHERE id=? AND household_id=?
            """, (paid_by, datetime.now(ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))).replace(tzinfo=None).isoformat(), bill_id, household_id))

            recurrence = bill['recurrence']
            if recurrence and recurrence != 'once':
                due = date.fromisoformat(bill['due_date'])
                if recurrence == 'monthly':
                    next_month = due.month + 1
                    next_year = due.year + (next_month - 1) // 12
                    next_month = ((next_month - 1) % 12) + 1
                    import calendar as cal_mod
                    max_day = cal_mod.monthrange(next_year, next_month)[1]
                    next_due = date(next_year, next_month, min(due.day, max_day))
                elif recurrence == 'quarterly':
                    next_month = due.month + 3
                    next_year = due.year + (next_month - 1) // 12
                    next_month = ((next_month - 1) % 12) + 1
                    import calendar as cal_mod
                    max_day = cal_mod.monthrange(next_year, next_month)[1]
                    next_due = date(next_year, next_month, min(due.day, max_day))
                elif recurrence == 'yearly':
                    try:
                        next_due = date(due.year + 1, due.month, due.day)
                    except ValueError:
                        next_due = date(due.year + 1, due.month, 28)
                else:
                    next_due = None

                if next_due:
                    conn.execute("""
                        INSERT INTO bills (name, amount, due_date, recurrence, category, notes, split_type, split_between, household_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (bill['name'], bill['amount'], next_due.isoformat(),
                          bill['recurrence'], bill['category'], bill['notes'],
                          bill['split_type'] or 'equal', bill['split_between'] or '[]', household_id))

            conn.commit()
        await manager.broadcast({"type": "bills_updated"}, household_id=household_id)

        # Notify household that a bill was paid
        try:
            bill_name = bill['name']
            amount = bill['amount']
            for person in get_people(household_id=household_id):
                if person != paid_by:
                    send_push_to_person_bg(
                        person,
                        "Huddle: Bill paid",
                        f"{paid_by or 'Someone'} paid {bill_name} (${amount:.2f})",
                        "bill-paid",
                        household_id=household_id,
                        module="bills",
                    )
        except Exception as push_err:
            logger.warning("Bill payment push notification failed: %s", push_err)

        return {"message": "Bill marked as paid"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in pay_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in pay_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/bills/{bill_id}/unpay")
async def unpay_bill(bill_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Mark a bill as unpaid."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            bill = conn.execute("SELECT * FROM bills WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (bill_id, household_id)).fetchone()
            if not bill:
                raise HTTPException(status_code=404, detail="Bill not found")
            conn.execute("""
                UPDATE bills SET paid=0, paid_by=NULL, paid_at=NULL WHERE id=? AND household_id=?
            """, (bill_id, household_id))
            conn.commit()
        await manager.broadcast({"type": "bills_updated"}, household_id=household_id)
        return {"message": "Bill marked as unpaid"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in unpay_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in unpay_bill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/bills/balances")
def get_balances(tenant: TenantContext = Depends(get_current_user)):
    """Calculate who owes whom based on paid bills with splits."""
    household_id = tenant.household_id
    try:
        people = get_people(household_id=household_id)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM bills WHERE household_id = ? AND paid = 1 AND paid_by IS NOT NULL AND deleted_at IS NULL",
                (household_id,)
            ).fetchall()

        # Build net debts between pairs
        # debts[A][B] = amount A owes B
        debts = defaultdict(lambda: defaultdict(float))

        for row in rows:
            bill = dict(row)
            payer = bill.get('paid_by')
            amount = bill.get('amount') or 0
            split_type = bill.get('split_type') or 'equal'
            if amount <= 0 or not payer:
                continue

            try:
                split_between = json.loads(bill.get('split_between') or '[]')
            except (json.JSONDecodeError, TypeError):
                split_between = []

            # Default: split between all household members
            if not split_between:
                split_between = people

            if split_type == 'single' or len(split_between) <= 1:
                continue  # No split needed

            per_person = amount / len(split_between)
            for person in split_between:
                if person != payer:
                    debts[person][payer] += per_person

        # Net out debts between pairs
        balances = []
        processed = set()
        all_people = set(debts.keys())
        for d in debts.values():
            all_people.update(d.keys())

        for a in sorted(all_people):
            for b in sorted(all_people):
                if a >= b:
                    continue
                pair = (a, b)
                if pair in processed:
                    continue
                processed.add(pair)
                a_owes_b = debts[a][b]
                b_owes_a = debts[b][a]
                net = a_owes_b - b_owes_a
                if abs(net) > 0.01:
                    if net > 0:
                        balances.append({"from": a, "to": b, "amount": round(net, 2)})
                    else:
                        balances.append({"from": b, "to": a, "amount": round(-net, 2)})

        # Calculate current user's net balance
        my_name = tenant.display_name
        my_balance = 0.0
        for b in balances:
            if b["from"] == my_name:
                my_balance -= b["amount"]
            elif b["to"] == my_name:
                my_balance += b["amount"]

        return {
            "balances": balances,
            "my_balance": round(my_balance, 2),
            "my_name": my_name,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_balances: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_balances: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
