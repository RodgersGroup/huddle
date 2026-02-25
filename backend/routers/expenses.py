"""Expense Splitting API — track shared expenses, settlements, and recurring costs."""
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

EXPENSE_CATEGORIES = ('groceries', 'utilities', 'rent', 'household', 'eating_out', 'transport', 'entertainment', 'health', 'other')
SHARED_COST_CATEGORIES = ('rent', 'utilities', 'internet', 'insurance', 'other')
SETTLEMENT_METHODS = ('payid', 'bank_transfer', 'cash', 'beem_it', 'other')


def _calculate_splits(amount, split_type, split_data, paid_by, household_id):
    """Calculate per-person split amounts based on split type.

    Returns a list of dicts: [{"person": "Alice", "amount": 15.00}, ...]
    Raises HTTPException on validation errors.
    """
    if split_type == 'equal':
        # If split_data lists specific people, use those; otherwise all household members
        if split_data and isinstance(split_data, list) and len(split_data) > 0:
            people = split_data
        else:
            people = get_people(household_id=household_id)
        if not people:
            logger.warning("Validation failed in _calculate_splits: %s", "No household members found for equal split")
            raise HTTPException(status_code=400, detail="No household members found for equal split")
        per_person = round(amount / len(people), 2)
        return [{"person": p, "amount": per_person} for p in people]

    elif split_type == 'exact':
        if not split_data or not isinstance(split_data, dict):
            logger.warning("Validation failed in _calculate_splits: %s", "split_data must be a dict for exact split")
            raise HTTPException(status_code=400, detail="split_data must be a dict for exact split")
        total = round(sum(split_data.values()), 2)
        if abs(total - amount) > 0.01:
            logger.warning("Validation failed in _calculate_splits: %s", f"Exact split amounts ({total}) must sum to total ({amount})")
            raise HTTPException(status_code=400, detail=f"Exact split amounts ({total}) must sum to total ({amount})")
        return [{"person": p, "amount": round(a, 2)} for p, a in split_data.items()]

    elif split_type == 'percentage':
        if not split_data or not isinstance(split_data, dict):
            logger.warning("Validation failed in _calculate_splits: %s", "split_data must be a dict for percentage split")
            raise HTTPException(status_code=400, detail="split_data must be a dict for percentage split")
        total_pct = round(sum(split_data.values()), 2)
        if abs(total_pct - 100) > 0.01:
            logger.warning("Validation failed in _calculate_splits: %s", f"Percentages ({total_pct}) must sum to 100")
            raise HTTPException(status_code=400, detail=f"Percentages ({total_pct}) must sum to 100")
        return [{"person": p, "amount": round(amount * pct / 100, 2)} for p, pct in split_data.items()]

    elif split_type == 'shares':
        if not split_data or not isinstance(split_data, dict):
            logger.warning("Validation failed in _calculate_splits: %s", "split_data must be a dict for shares split")
            raise HTTPException(status_code=400, detail="split_data must be a dict for shares split")
        total_shares = sum(split_data.values())
        if total_shares <= 0:
            logger.warning("Validation failed in _calculate_splits: %s", "Total shares must be greater than 0")
            raise HTTPException(status_code=400, detail="Total shares must be greater than 0")
        return [{"person": p, "amount": round(amount * s / total_shares, 2)} for p, s in split_data.items()]

    else:
        logger.warning("Validation failed in _calculate_splits: %s", f"Invalid split_type: {split_type}")
        raise HTTPException(status_code=400, detail=f"Invalid split_type: {split_type}")


# ─── Balances (registered before /{id} routes) ───────────────────────────────


@router.get("/api/expenses/balances")
def get_balances(tenant: TenantContext = Depends(get_current_user)):
    """Calculate net balances and minimum settlement transfers."""
    household_id = tenant.household_id
    try:
        people = get_people(household_id=household_id)
        with get_db() as conn:
            # 1. What each person owes (their share of expenses)
            split_rows = conn.execute(
                "SELECT person, SUM(amount) as total FROM expense_splits WHERE household_id = ? GROUP BY person",
                (household_id,)
            ).fetchall()
            shares = {row['person']: row['total'] for row in split_rows}

            # 2. What each person paid out
            paid_rows = conn.execute(
                "SELECT paid_by, SUM(amount) as total FROM expenses WHERE household_id = ? GROUP BY paid_by",
                (household_id,)
            ).fetchall()
            paid = {row['paid_by']: row['total'] for row in paid_rows}

            # 3. Settlements sent and received
            sent_rows = conn.execute(
                "SELECT paid_by, SUM(amount) as total FROM settlements WHERE household_id = ? GROUP BY paid_by",
                (household_id,)
            ).fetchall()
            settlements_sent = {row['paid_by']: row['total'] for row in sent_rows}

            received_rows = conn.execute(
                "SELECT paid_to, SUM(amount) as total FROM settlements WHERE household_id = ? GROUP BY paid_to",
                (household_id,)
            ).fetchall()
            settlements_received = {row['paid_to']: row['total'] for row in received_rows}

        # 4. Calculate net for each person
        all_people = set(people) | set(shares.keys()) | set(paid.keys()) | set(settlements_sent.keys()) | set(settlements_received.keys())
        balances = []
        for person in sorted(all_people):
            net = (
                paid.get(person, 0)
                - shares.get(person, 0)
                + settlements_received.get(person, 0)
                - settlements_sent.get(person, 0)
            )
            balances.append({"person": person, "net": round(net, 2)})

        # 5. Simplify into minimum transfers (greedy algorithm)
        debtors = []  # negative net = owes money
        creditors = []  # positive net = owed money
        for b in balances:
            if b['net'] < -0.01:
                debtors.append({"person": b['person'], "amount": -b['net']})
            elif b['net'] > 0.01:
                creditors.append({"person": b['person'], "amount": b['net']})

        # Sort debtors ascending (biggest debt first), creditors descending (biggest credit first)
        debtors.sort(key=lambda x: -x['amount'])
        creditors.sort(key=lambda x: -x['amount'])

        settlements_needed = []
        i, j = 0, 0
        while i < len(debtors) and j < len(creditors):
            transfer = min(debtors[i]['amount'], creditors[j]['amount'])
            if transfer > 0.01:
                settlements_needed.append({
                    "from": debtors[i]['person'],
                    "to": creditors[j]['person'],
                    "amount": round(transfer, 2),
                })
            debtors[i]['amount'] -= transfer
            creditors[j]['amount'] -= transfer
            if debtors[i]['amount'] < 0.01:
                i += 1
            if creditors[j]['amount'] < 0.01:
                j += 1

        return {
            "balances": balances,
            "settlements_needed": settlements_needed,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_balances: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_balances: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/expenses/summary")
def get_summary(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Monthly expense summary."""
    household_id = tenant.household_id
    try:
        month = request.query_params.get('month', datetime.now().strftime('%Y-%m'))
        # Validate month format
        try:
            datetime.strptime(month, '%Y-%m')
        except ValueError:
            logger.warning("Validation failed in get_summary: %s", "Invalid month format, use YYYY-MM")
            raise HTTPException(status_code=400, detail="Invalid month format, use YYYY-MM")

        month_start = f"{month}-01"
        month_end = f"{month}-31"

        with get_db() as conn:
            # Total spent this month
            total_row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE household_id = ? AND date >= ? AND date <= ?",
                (household_id, month_start, month_end)
            ).fetchone()
            total_spent = round(total_row['total'], 2)

            # Per-person spend (who paid)
            person_rows = conn.execute(
                "SELECT paid_by, SUM(amount) as total FROM expenses WHERE household_id = ? AND date >= ? AND date <= ? GROUP BY paid_by ORDER BY total DESC",
                (household_id, month_start, month_end)
            ).fetchall()
            per_person = [{"person": row['paid_by'], "amount": round(row['total'], 2)} for row in person_rows]

            # Per-category breakdown
            category_rows = conn.execute(
                "SELECT category, SUM(amount) as total, COUNT(*) as count FROM expenses WHERE household_id = ? AND date >= ? AND date <= ? GROUP BY category ORDER BY total DESC",
                (household_id, month_start, month_end)
            ).fetchall()
            per_category = [{"category": row['category'], "amount": round(row['total'], 2), "count": row['count']} for row in category_rows]

        return {
            "month": month,
            "total_spent": total_spent,
            "per_person": per_person,
            "per_category": per_category,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_summary: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_summary: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Expenses CRUD ────────────────────────────────────────────────────────────


@router.get("/api/expenses")
def get_expenses(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """List expenses with optional filters, newest first."""
    household_id = tenant.household_id
    try:
        category = request.query_params.get('category')
        person = request.query_params.get('person')
        date_from = request.query_params.get('from')
        date_to = request.query_params.get('to')

        with get_db() as conn:
            query = "SELECT * FROM expenses WHERE household_id = ?"
            params = [household_id]

            if category:
                query += " AND category = ?"
                params.append(category)
            if person:
                query += " AND paid_by = ?"
                params.append(person)
            if date_from:
                query += " AND date >= ?"
                params.append(date_from)
            if date_to:
                query += " AND date <= ?"
                params.append(date_to)

            query += " ORDER BY date DESC, created_at DESC"
            rows = conn.execute(query, params).fetchall()

            expenses = []
            for row in rows:
                expense = dict(row)
                # Parse split_data from JSON string
                if expense.get('split_data'):
                    try:
                        expense['split_data'] = json.loads(expense['split_data'])
                    except (json.JSONDecodeError, TypeError):
                        pass
                # Fetch splits for this expense
                splits = conn.execute(
                    "SELECT person, amount FROM expense_splits WHERE expense_id = ? AND household_id = ?",
                    (expense['id'], household_id)
                ).fetchall()
                expense['splits'] = [dict(s) for s in splits]
                expenses.append(expense)

            return {"expenses": expenses}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_expenses: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_expenses: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/expenses")
async def create_expense(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Create a new expense with splits."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        description = data.get('description', '').strip() if isinstance(data.get('description'), str) else ''
        if not description:
            logger.warning("Validation failed in create_expense: %s", "Description is required")
            raise HTTPException(status_code=400, detail="Description is required")
        if len(description) > 200:
            logger.warning("Validation failed in create_expense: %s", "Description is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Description is too long (max 200 characters)")

        amount = data.get('amount')
        if amount is None:
            logger.warning("Validation failed in create_expense: %s", "Amount is required")
            raise HTTPException(status_code=400, detail="Amount is required")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            logger.warning("Validation failed in create_expense: %s", "Amount must be a valid number")
            raise HTTPException(status_code=400, detail="Amount must be a valid number")
        if amount <= 0:
            logger.warning("Validation failed in create_expense: %s", "Amount must be greater than 0")
            raise HTTPException(status_code=400, detail="Amount must be greater than 0")
        amount = round(amount, 2)

        paid_by = data.get('paid_by', '').strip() if isinstance(data.get('paid_by'), str) else ''
        if not paid_by:
            logger.warning("Validation failed in create_expense: %s", "paid_by is required")
            raise HTTPException(status_code=400, detail="paid_by is required")

        expense_date = data.get('date', '').strip() if isinstance(data.get('date'), str) else ''
        if not expense_date:
            logger.warning("Validation failed in create_expense: %s", "Date is required")
            raise HTTPException(status_code=400, detail="Date is required")

        split_type = data.get('split_type', 'equal')
        if split_type not in ('equal', 'exact', 'percentage', 'shares'):
            logger.warning("Validation failed in create_expense: %s", f"Invalid split_type: {split_type}")
            raise HTTPException(status_code=400, detail=f"Invalid split_type: {split_type}")

        split_data = data.get('split_data')
        category = data.get('category', 'other')
        if category not in EXPENSE_CATEGORIES:
            category = 'other'

        receipt_note = data.get('receipt_note', '')
        if isinstance(receipt_note, str) and len(receipt_note) > 500:
            logger.warning("Validation failed in create_expense: %s", "Receipt note is too long (max 500 characters)")
            raise HTTPException(status_code=400, detail="Receipt note is too long (max 500 characters)")

        currency = data.get('currency', 'AUD')
        # --- End validation ---

        # Calculate splits
        splits = _calculate_splits(amount, split_type, split_data, paid_by, household_id)

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO expenses (description, amount, currency, paid_by, split_type, split_data, category, receipt_note, date, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                description,
                amount,
                currency,
                paid_by,
                split_type,
                json.dumps(split_data) if split_data else None,
                category,
                receipt_note,
                expense_date,
                household_id,
            ))
            expense_id = cursor.lastrowid

            for split in splits:
                conn.execute("""
                    INSERT INTO expense_splits (expense_id, person, amount, household_id)
                    VALUES (?, ?, ?, ?)
                """, (expense_id, split['person'], split['amount'], household_id))

            conn.commit()

        await manager.broadcast({"type": "expenses_updated"}, household_id=household_id)

        # Push notification to each person with a split (except the payer)
        for split in splits:
            if split['person'] != paid_by:
                send_push_to_person_bg(
                    split['person'],
                    "Huddle: Expense logged",
                    f"{paid_by} logged ${amount:.2f} for {description} — your share is ${split['amount']:.2f}",
                    "expense",
                    household_id=household_id,
                )

        return {"id": expense_id, "message": "Expense created", "splits": splits}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_expense: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_expense: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/expenses/{expense_id}")
async def update_expense(expense_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update an expense. Only the person who logged it or a manager can update."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM expenses WHERE id = ? AND household_id = ?",
                (expense_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Expense not found")

            # Permission check: only the logger or a manager can update
            if existing['paid_by'] != tenant.display_name and tenant.role != 'manager':
                raise HTTPException(status_code=403, detail="Only the person who logged this expense or a manager can update it")

            # --- Input validation ---
            description = data.get('description', existing['description'])
            if isinstance(description, str):
                description = description.strip()
            if not description:
                logger.warning("Validation failed in update_expense: %s", "Description is required")
                raise HTTPException(status_code=400, detail="Description is required")
            if isinstance(description, str) and len(description) > 200:
                logger.warning("Validation failed in update_expense: %s", "Description is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Description is too long (max 200 characters)")

            amount = data.get('amount', existing['amount'])
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                logger.warning("Validation failed in update_expense: %s", "Amount must be a valid number")
                raise HTTPException(status_code=400, detail="Amount must be a valid number")
            if amount <= 0:
                logger.warning("Validation failed in update_expense: %s", "Amount must be greater than 0")
                raise HTTPException(status_code=400, detail="Amount must be greater than 0")
            amount = round(amount, 2)

            paid_by = data.get('paid_by', existing['paid_by'])
            if isinstance(paid_by, str):
                paid_by = paid_by.strip()
            if not paid_by:
                logger.warning("Validation failed in update_expense: %s", "paid_by is required")
                raise HTTPException(status_code=400, detail="paid_by is required")

            expense_date = data.get('date', existing['date'])
            split_type = data.get('split_type', existing['split_type'])
            if split_type not in ('equal', 'exact', 'percentage', 'shares'):
                logger.warning("Validation failed in update_expense: %s", f"Invalid split_type: {split_type}")
                raise HTTPException(status_code=400, detail=f"Invalid split_type: {split_type}")

            split_data = data.get('split_data')
            if split_data is None and existing['split_data']:
                try:
                    split_data = json.loads(existing['split_data'])
                except (json.JSONDecodeError, TypeError):
                    split_data = None

            category = data.get('category', existing['category'])
            if category not in EXPENSE_CATEGORIES:
                category = 'other'

            receipt_note = data.get('receipt_note', existing['receipt_note'] or '')
            if isinstance(receipt_note, str) and len(receipt_note) > 500:
                logger.warning("Validation failed in update_expense: %s", "Receipt note is too long (max 500 characters)")
                raise HTTPException(status_code=400, detail="Receipt note is too long (max 500 characters)")

            currency = data.get('currency', existing['currency'])
            # --- End validation ---

            # Recalculate splits
            splits = _calculate_splits(amount, split_type, split_data, paid_by, household_id)

            conn.execute("""
                UPDATE expenses SET description=?, amount=?, currency=?, paid_by=?, split_type=?, split_data=?, category=?, receipt_note=?, date=?
                WHERE id=? AND household_id=?
            """, (
                description,
                amount,
                currency,
                paid_by,
                split_type,
                json.dumps(split_data) if split_data else None,
                category,
                receipt_note,
                expense_date,
                expense_id,
                household_id,
            ))

            # Delete old splits and insert new ones
            conn.execute("DELETE FROM expense_splits WHERE expense_id = ? AND household_id = ?", (expense_id, household_id))
            for split in splits:
                conn.execute("""
                    INSERT INTO expense_splits (expense_id, person, amount, household_id)
                    VALUES (?, ?, ?, ?)
                """, (expense_id, split['person'], split['amount'], household_id))

            conn.commit()

        await manager.broadcast({"type": "expenses_updated"}, household_id=household_id)
        return {"message": "Expense updated", "splits": splits}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_expense: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_expense: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/expenses/{expense_id}")
async def delete_expense(expense_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete an expense. Only the logger or a manager can delete."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM expenses WHERE id = ? AND household_id = ?",
                (expense_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Expense not found")

            if existing['paid_by'] != tenant.display_name and tenant.role != 'manager':
                raise HTTPException(status_code=403, detail="Only the person who logged this expense or a manager can delete it")

            conn.execute("DELETE FROM expenses WHERE id = ? AND household_id = ?", (expense_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "expenses_updated"}, household_id=household_id)
        return {"message": "Expense deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_expense: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_expense: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Settlements ──────────────────────────────────────────────────────────────


@router.post("/api/settlements")
async def create_settlement(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Record a settlement payment between household members."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        paid_by = data.get('paid_by', '').strip() if isinstance(data.get('paid_by'), str) else ''
        if not paid_by:
            logger.warning("Validation failed in create_settlement: %s", "paid_by is required")
            raise HTTPException(status_code=400, detail="paid_by is required")

        paid_to = data.get('paid_to', '').strip() if isinstance(data.get('paid_to'), str) else ''
        if not paid_to:
            logger.warning("Validation failed in create_settlement: %s", "paid_to is required")
            raise HTTPException(status_code=400, detail="paid_to is required")

        if paid_by == paid_to:
            logger.warning("Validation failed in create_settlement: %s", "Cannot settle with yourself")
            raise HTTPException(status_code=400, detail="Cannot settle with yourself")

        amount = data.get('amount')
        if amount is None:
            logger.warning("Validation failed in create_settlement: %s", "Amount is required")
            raise HTTPException(status_code=400, detail="Amount is required")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            logger.warning("Validation failed in create_settlement: %s", "Amount must be a valid number")
            raise HTTPException(status_code=400, detail="Amount must be a valid number")
        if amount <= 0:
            logger.warning("Validation failed in create_settlement: %s", "Amount must be greater than 0")
            raise HTTPException(status_code=400, detail="Amount must be greater than 0")
        amount = round(amount, 2)

        settlement_date = data.get('date', '').strip() if isinstance(data.get('date'), str) else ''
        if not settlement_date:
            logger.warning("Validation failed in create_settlement: %s", "Date is required")
            raise HTTPException(status_code=400, detail="Date is required")

        method = data.get('method', '')
        if method and method not in SETTLEMENT_METHODS:
            method = 'other'

        note = data.get('note', '')
        if isinstance(note, str) and len(note) > 500:
            logger.warning("Validation failed in create_settlement: %s", "Note is too long (max 500 characters)")
            raise HTTPException(status_code=400, detail="Note is too long (max 500 characters)")
        # --- End validation ---

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO settlements (paid_by, paid_to, amount, method, note, date, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (paid_by, paid_to, amount, method, note, settlement_date, household_id))
            conn.commit()
            settlement_id = cursor.lastrowid

        await manager.broadcast({"type": "expenses_updated"}, household_id=household_id)

        # Push notification to the recipient
        send_push_to_person_bg(
            paid_to,
            "Huddle: Settlement received",
            f"{paid_by} settled ${amount:.2f} with you",
            "settlement",
            household_id=household_id,
        )

        return {"id": settlement_id, "message": "Settlement recorded"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_settlement: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_settlement: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/settlements")
def get_settlements(tenant: TenantContext = Depends(get_current_user)):
    """List all settlements, newest first."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM settlements WHERE household_id = ? ORDER BY date DESC, created_at DESC",
                (household_id,)
            ).fetchall()
            return {"settlements": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_settlements: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_settlements: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/settlements/{settlement_id}")
async def delete_settlement(settlement_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a settlement. Only the recorder or a manager can delete."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM settlements WHERE id = ? AND household_id = ?",
                (settlement_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Settlement not found")

            if existing['paid_by'] != tenant.display_name and tenant.role != 'manager':
                raise HTTPException(status_code=403, detail="Only the person who recorded this settlement or a manager can delete it")

            conn.execute("DELETE FROM settlements WHERE id = ? AND household_id = ?", (settlement_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "expenses_updated"}, household_id=household_id)
        return {"message": "Settlement deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_settlement: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_settlement: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Shared Costs ─────────────────────────────────────────────────────────────


@router.get("/api/shared-costs")
def get_shared_costs(tenant: TenantContext = Depends(get_current_user)):
    """List recurring shared costs."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM shared_costs WHERE household_id = ? ORDER BY created_at DESC",
                (household_id,)
            ).fetchall()
            costs = []
            for row in rows:
                cost = dict(row)
                if cost.get('split_data'):
                    try:
                        cost['split_data'] = json.loads(cost['split_data'])
                    except (json.JSONDecodeError, TypeError):
                        pass
                costs.append(cost)
            return {"shared_costs": costs}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_shared_costs: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_shared_costs: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/shared-costs")
async def create_shared_cost(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Create a recurring shared cost."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
        if not name:
            logger.warning("Validation failed in create_shared_cost: %s", "Name is required")
            raise HTTPException(status_code=400, detail="Name is required")
        if len(name) > 200:
            logger.warning("Validation failed in create_shared_cost: %s", "Name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")

        amount = data.get('amount')
        if amount is None:
            logger.warning("Validation failed in create_shared_cost: %s", "Amount is required")
            raise HTTPException(status_code=400, detail="Amount is required")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            logger.warning("Validation failed in create_shared_cost: %s", "Amount must be a valid number")
            raise HTTPException(status_code=400, detail="Amount must be a valid number")
        if amount <= 0:
            logger.warning("Validation failed in create_shared_cost: %s", "Amount must be greater than 0")
            raise HTTPException(status_code=400, detail="Amount must be greater than 0")
        amount = round(amount, 2)

        split_type = data.get('split_type', 'equal')
        if split_type not in ('equal', 'exact', 'percentage', 'shares'):
            split_type = 'equal'

        split_data = data.get('split_data')
        frequency = data.get('frequency', 'monthly')
        if frequency not in ('weekly', 'fortnightly', 'monthly', 'quarterly', 'yearly'):
            frequency = 'monthly'

        due_day = data.get('due_day')
        if due_day is not None:
            try:
                due_day = int(due_day)
                if due_day < 1 or due_day > 31:
                    due_day = None
            except (TypeError, ValueError):
                due_day = None

        category = data.get('category', 'rent')
        if category not in SHARED_COST_CATEGORIES:
            category = 'other'
        # --- End validation ---

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO shared_costs (name, amount, split_type, split_data, frequency, due_day, category, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                amount,
                split_type,
                json.dumps(split_data) if split_data else None,
                frequency,
                due_day,
                category,
                household_id,
            ))
            conn.commit()
            cost_id = cursor.lastrowid

        await manager.broadcast({"type": "expenses_updated"}, household_id=household_id)
        return {"id": cost_id, "message": "Shared cost created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_shared_cost: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_shared_cost: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/shared-costs/{cost_id}")
async def update_shared_cost(cost_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update a shared cost."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM shared_costs WHERE id = ? AND household_id = ?",
                (cost_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Shared cost not found")

            # --- Input validation ---
            name = data.get('name', existing['name'])
            if isinstance(name, str):
                name = name.strip()
            if not name:
                logger.warning("Validation failed in update_shared_cost: %s", "Name is required")
                raise HTTPException(status_code=400, detail="Name is required")
            if isinstance(name, str) and len(name) > 200:
                logger.warning("Validation failed in update_shared_cost: %s", "Name is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")

            amount = data.get('amount', existing['amount'])
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                logger.warning("Validation failed in update_shared_cost: %s", "Amount must be a valid number")
                raise HTTPException(status_code=400, detail="Amount must be a valid number")
            if amount <= 0:
                logger.warning("Validation failed in update_shared_cost: %s", "Amount must be greater than 0")
                raise HTTPException(status_code=400, detail="Amount must be greater than 0")
            amount = round(amount, 2)

            split_type = data.get('split_type', existing['split_type'])
            if split_type not in ('equal', 'exact', 'percentage', 'shares'):
                split_type = 'equal'

            split_data = data.get('split_data')
            if split_data is None and existing['split_data']:
                try:
                    split_data = json.loads(existing['split_data'])
                except (json.JSONDecodeError, TypeError):
                    split_data = None

            frequency = data.get('frequency', existing['frequency'])
            if frequency not in ('weekly', 'fortnightly', 'monthly', 'quarterly', 'yearly'):
                frequency = 'monthly'

            due_day = data.get('due_day', existing['due_day'])
            if due_day is not None:
                try:
                    due_day = int(due_day)
                    if due_day < 1 or due_day > 31:
                        due_day = None
                except (TypeError, ValueError):
                    due_day = None

            category = data.get('category', existing['category'])
            if category not in SHARED_COST_CATEGORIES:
                category = 'other'

            active = data.get('active', existing['active'])
            if isinstance(active, bool):
                active = 1 if active else 0
            # --- End validation ---

            conn.execute("""
                UPDATE shared_costs SET name=?, amount=?, split_type=?, split_data=?, frequency=?, due_day=?, category=?, active=?
                WHERE id=? AND household_id=?
            """, (
                name,
                amount,
                split_type,
                json.dumps(split_data) if split_data else None,
                frequency,
                due_day,
                category,
                active,
                cost_id,
                household_id,
            ))
            conn.commit()

        await manager.broadcast({"type": "expenses_updated"}, household_id=household_id)
        return {"message": "Shared cost updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_shared_cost: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_shared_cost: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/shared-costs/{cost_id}")
async def delete_shared_cost(cost_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a shared cost."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute(
                "DELETE FROM shared_costs WHERE id = ? AND household_id = ?",
                (cost_id, household_id)
            )
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Shared cost not found")

        await manager.broadcast({"type": "expenses_updated"}, household_id=household_id)
        return {"message": "Shared cost deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_shared_cost: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_shared_cost: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
