import logging
import sqlite3
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext
from datetime import datetime, date
from db import get_db
from settings import get_people
from websocket import manager
from push import send_push_to_person_bg

logger = logging.getLogger("huddle")

router = APIRouter()

MOVE_OUT_ITEMS = [
    {"name": "Return keys (all copies)", "checked": False, "note": ""},
    {"name": "Clean room thoroughly", "checked": False, "note": ""},
    {"name": "Fill any wall holes / remove hooks", "checked": False, "note": ""},
    {"name": "Clean share of common areas", "checked": False, "note": ""},
    {"name": "Redirect mail", "checked": False, "note": ""},
    {"name": "Update address with services", "checked": False, "note": ""},
    {"name": "Final meter readings (if applicable)", "checked": False, "note": ""},
    {"name": "Arrange bond refund split", "checked": False, "note": ""},
    {"name": "Remove from household", "checked": False, "note": ""},
]

MOVE_IN_ITEMS = [
    {"name": "Receive keys", "checked": False, "note": ""},
    {"name": "Condition report photos", "checked": False, "note": ""},
    {"name": "Set up mail", "checked": False, "note": ""},
    {"name": "Pay bond contribution", "checked": False, "note": ""},
    {"name": "Join household Huddle", "checked": False, "note": ""},
    {"name": "Acknowledge house rules", "checked": False, "note": ""},
    {"name": "Set up bill splitting", "checked": False, "note": ""},
]

VALID_RENT_FREQUENCIES = ('weekly', 'fortnightly', 'monthly')
VALID_BOND_LODGED = ('fair_trading', 'landlord', 'agent', 'other')
VALID_CHECKLIST_TYPES = ('move_in', 'move_out')


@router.get("/api/tenancy")
def get_tenancy(tenant: TenantContext = Depends(get_current_user)):
    """Get lease details + bond contributions + days remaining."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            tenancy = conn.execute(
                "SELECT * FROM tenancy WHERE household_id = ?", (household_id,)
            ).fetchone()
            tenancy_dict = dict(tenancy) if tenancy else None

            contributions = conn.execute(
                "SELECT * FROM bond_contributions WHERE household_id = ? ORDER BY paid_date ASC",
                (household_id,)
            ).fetchall()
            contributions_list = [dict(r) for r in contributions]

            days_remaining = None
            if tenancy_dict and tenancy_dict.get('lease_end'):
                try:
                    lease_end = date.fromisoformat(tenancy_dict['lease_end'])
                    days_remaining = (lease_end - date.today()).days
                except (ValueError, TypeError):
                    days_remaining = None

            return {
                "tenancy": tenancy_dict,
                "bond_contributions": contributions_list,
                "days_remaining": days_remaining,
            }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_tenancy: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_tenancy: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/tenancy")
async def create_or_update_tenancy(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Create or update lease details. Manager only."""
    household_id = tenant.household_id
    if tenant.role != "manager":
        raise HTTPException(status_code=403, detail="Manager only")
    try:
        data = await request.json()

        # --- Input validation ---
        if 'address' in data:
            address = data.get('address', '').strip() if isinstance(data.get('address'), str) else ''
            if len(address) > 500:
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Address is too long (max 500 characters)")
                raise HTTPException(status_code=400, detail="Address is too long (max 500 characters)")
            data['address'] = address

        if 'landlord_name' in data:
            landlord_name = data.get('landlord_name', '').strip() if isinstance(data.get('landlord_name'), str) else ''
            if len(landlord_name) > 200:
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Landlord name is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Landlord name is too long (max 200 characters)")
            data['landlord_name'] = landlord_name

        if 'landlord_contact' in data:
            landlord_contact = data.get('landlord_contact', '').strip() if isinstance(data.get('landlord_contact'), str) else ''
            if len(landlord_contact) > 200:
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Landlord contact is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Landlord contact is too long (max 200 characters)")
            data['landlord_contact'] = landlord_contact

        if 'real_estate_agent' in data:
            real_estate_agent = data.get('real_estate_agent', '').strip() if isinstance(data.get('real_estate_agent'), str) else ''
            if len(real_estate_agent) > 200:
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Real estate agent name is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Real estate agent name is too long (max 200 characters)")
            data['real_estate_agent'] = real_estate_agent

        if 'agent_contact' in data:
            agent_contact = data.get('agent_contact', '').strip() if isinstance(data.get('agent_contact'), str) else ''
            if len(agent_contact) > 200:
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Agent contact is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Agent contact is too long (max 200 characters)")
            data['agent_contact'] = agent_contact

        if 'bond_reference' in data:
            bond_reference = data.get('bond_reference', '').strip() if isinstance(data.get('bond_reference'), str) else ''
            if len(bond_reference) > 100:
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Bond reference is too long (max 100 characters)")
                raise HTTPException(status_code=400, detail="Bond reference is too long (max 100 characters)")
            data['bond_reference'] = bond_reference

        if 'notes' in data:
            notes = data.get('notes', '').strip() if isinstance(data.get('notes'), str) else ''
            if len(notes) > 2000:
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Notes are too long (max 2000 characters)")
                raise HTTPException(status_code=400, detail="Notes are too long (max 2000 characters)")
            data['notes'] = notes

        if 'rent_amount' in data and data['rent_amount'] is not None:
            try:
                rent_amount = float(data['rent_amount'])
                if rent_amount < 0 or rent_amount > 999999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Rent amount must be a valid number (0 to 999999999)")
                raise HTTPException(status_code=400, detail="Rent amount must be a valid number (0 to 999999999)")
            data['rent_amount'] = rent_amount

        if 'rent_frequency' in data:
            if data['rent_frequency'] not in VALID_RENT_FREQUENCIES:
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Rent frequency must be weekly, fortnightly, or monthly")
                raise HTTPException(status_code=400, detail="Rent frequency must be weekly, fortnightly, or monthly")

        if 'bond_total' in data and data['bond_total'] is not None:
            try:
                bond_total = float(data['bond_total'])
                if bond_total < 0 or bond_total > 999999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Bond total must be a valid number (0 to 999999999)")
                raise HTTPException(status_code=400, detail="Bond total must be a valid number (0 to 999999999)")
            data['bond_total'] = bond_total

        if 'bond_lodged_with' in data:
            if data['bond_lodged_with'] not in VALID_BOND_LODGED:
                logger.warning("Validation failed in create_or_update_tenancy: %s", "Bond lodged with must be fair_trading, landlord, agent, or other")
                raise HTTPException(status_code=400, detail="Bond lodged with must be fair_trading, landlord, agent, or other")
        # --- End validation ---

        now = datetime.now().isoformat()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM tenancy WHERE household_id = ?", (household_id,)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE tenancy SET address=?, landlord_name=?, landlord_contact=?,
                    real_estate_agent=?, agent_contact=?, lease_start=?, lease_end=?,
                    rent_amount=?, rent_frequency=?, bond_total=?, bond_lodged_with=?,
                    bond_reference=?, notes=?, updated_at=?
                    WHERE household_id=?
                """, (
                    data.get('address', existing['address']),
                    data.get('landlord_name', existing['landlord_name']),
                    data.get('landlord_contact', existing['landlord_contact']),
                    data.get('real_estate_agent', existing['real_estate_agent']),
                    data.get('agent_contact', existing['agent_contact']),
                    data.get('lease_start', existing['lease_start']),
                    data.get('lease_end', existing['lease_end']),
                    data.get('rent_amount', existing['rent_amount']),
                    data.get('rent_frequency', existing['rent_frequency']),
                    data.get('bond_total', existing['bond_total']),
                    data.get('bond_lodged_with', existing['bond_lodged_with']),
                    data.get('bond_reference', existing['bond_reference']),
                    data.get('notes', existing['notes']),
                    now,
                    household_id
                ))
                conn.commit()
                await manager.broadcast({"type": "tenancy_updated"}, household_id=household_id)
                return {"message": "Tenancy updated"}
            else:
                cursor = conn.execute("""
                    INSERT INTO tenancy (address, landlord_name, landlord_contact,
                    real_estate_agent, agent_contact, lease_start, lease_end,
                    rent_amount, rent_frequency, bond_total, bond_lodged_with,
                    bond_reference, notes, created_at, updated_at, household_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    data.get('address', ''),
                    data.get('landlord_name', ''),
                    data.get('landlord_contact', ''),
                    data.get('real_estate_agent', ''),
                    data.get('agent_contact', ''),
                    data.get('lease_start'),
                    data.get('lease_end'),
                    data.get('rent_amount'),
                    data.get('rent_frequency', 'weekly'),
                    data.get('bond_total'),
                    data.get('bond_lodged_with', 'fair_trading'),
                    data.get('bond_reference', ''),
                    data.get('notes', ''),
                    now,
                    now,
                    household_id
                ))
                conn.commit()
                tenancy_id = cursor.lastrowid
                await manager.broadcast({"type": "tenancy_updated"}, household_id=household_id)
                return {"id": tenancy_id, "message": "Tenancy created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_or_update_tenancy: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_or_update_tenancy: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/tenancy/bond")
def get_bond(tenant: TenantContext = Depends(get_current_user)):
    """Bond breakdown: list of contributions with total."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            tenancy = conn.execute(
                "SELECT bond_total FROM tenancy WHERE household_id = ?", (household_id,)
            ).fetchone()
            bond_total = tenancy['bond_total'] if tenancy else None

            contributions = conn.execute(
                "SELECT * FROM bond_contributions WHERE household_id = ? ORDER BY paid_date ASC",
                (household_id,)
            ).fetchall()
            contributions_list = [dict(r) for r in contributions]

            contributions_total = sum(c['amount'] for c in contributions_list if c.get('amount'))

            return {
                "bond_total": bond_total,
                "contributions": contributions_list,
                "contributions_total": round(contributions_total, 2),
            }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_bond: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_bond: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/tenancy/bond")
async def add_bond_contribution(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Add a bond contribution."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        person = data.get('person', '').strip() if isinstance(data.get('person'), str) else ''
        if not person:
            logger.warning("Validation failed in add_bond_contribution: %s", "Person is required")
            raise HTTPException(status_code=400, detail="Person is required")
        if len(person) > 200:
            logger.warning("Validation failed in add_bond_contribution: %s", "Person name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Person name is too long (max 200 characters)")
        data['person'] = person

        amount = data.get('amount')
        if amount is None:
            logger.warning("Validation failed in add_bond_contribution: %s", "Amount is required")
            raise HTTPException(status_code=400, detail="Amount is required")
        try:
            amount = float(amount)
            if amount <= 0 or amount > 999999999:
                raise ValueError
        except (TypeError, ValueError):
            logger.warning("Validation failed in add_bond_contribution: %s", "Amount must be a positive number")
            raise HTTPException(status_code=400, detail="Amount must be a positive number")
        data['amount'] = amount

        if 'notes' in data:
            notes = data.get('notes', '')
            if isinstance(notes, str) and len(notes) > 2000:
                logger.warning("Validation failed in add_bond_contribution: %s", "Notes are too long (max 2000 characters)")
                raise HTTPException(status_code=400, detail="Notes are too long (max 2000 characters)")
        # --- End validation ---

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO bond_contributions (person, amount, paid_date, notes, household_id)
                VALUES (?, ?, ?, ?, ?)
            """, (
                data['person'],
                data['amount'],
                data.get('paid_date'),
                data.get('notes', ''),
                household_id
            ))
            conn.commit()
            contribution_id = cursor.lastrowid
        await manager.broadcast({"type": "tenancy_updated"}, household_id=household_id)
        return {"id": contribution_id, "message": "Bond contribution added"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in add_bond_contribution: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in add_bond_contribution: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/tenancy/bond/{contribution_id}")
async def delete_bond_contribution(contribution_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Remove bond contribution. Manager only."""
    household_id = tenant.household_id
    if tenant.role != "manager":
        raise HTTPException(status_code=403, detail="Manager only")
    try:
        with get_db() as conn:
            result = conn.execute(
                "DELETE FROM bond_contributions WHERE id = ? AND household_id = ?",
                (contribution_id, household_id)
            )
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Bond contribution not found")
        await manager.broadcast({"type": "tenancy_updated"}, household_id=household_id)
        return {"message": "Bond contribution deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_bond_contribution: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_bond_contribution: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/tenancy/move-checklist")
async def create_move_checklist(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Create a move-in or move-out checklist for a person."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        person = data.get('person', '').strip() if isinstance(data.get('person'), str) else ''
        if not person:
            logger.warning("Validation failed in create_move_checklist: %s", "Person is required")
            raise HTTPException(status_code=400, detail="Person is required")
        if len(person) > 200:
            logger.warning("Validation failed in create_move_checklist: %s", "Person name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Person name is too long (max 200 characters)")
        data['person'] = person

        checklist_type = data.get('checklist_type', '').strip() if isinstance(data.get('checklist_type'), str) else ''
        if checklist_type not in VALID_CHECKLIST_TYPES:
            logger.warning("Validation failed in create_move_checklist: %s", "Checklist type must be move_in or move_out")
            raise HTTPException(status_code=400, detail="Checklist type must be move_in or move_out")
        data['checklist_type'] = checklist_type
        # --- End validation ---

        if checklist_type == 'move_out':
            items = [dict(item) for item in MOVE_OUT_ITEMS]
        else:
            items = [dict(item) for item in MOVE_IN_ITEMS]

        items_json = json.dumps(items)
        now = datetime.now().isoformat()

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO move_checklists (person, checklist_type, items, status, created_at, household_id)
                VALUES (?, ?, ?, 'in_progress', ?, ?)
            """, (
                data['person'],
                data['checklist_type'],
                items_json,
                now,
                household_id
            ))
            conn.commit()
            checklist_id = cursor.lastrowid

        await manager.broadcast({"type": "tenancy_updated"}, household_id=household_id)

        label = "move-out" if checklist_type == 'move_out' else "move-in"
        item_count = len(items)
        send_push_to_person_bg(
            person,
            "Huddle: Move Checklist",
            f"Your {label} checklist is ready \u2014 {item_count} items to complete",
            "move-checklist",
            household_id=household_id
        )

        return {"id": checklist_id, "message": "Move checklist created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_move_checklist: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_move_checklist: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/tenancy/move-checklists")
def list_move_checklists(tenant: TenantContext = Depends(get_current_user)):
    """List all checklists for the household (newest first)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM move_checklists WHERE household_id = ? ORDER BY created_at DESC",
                (household_id,)
            ).fetchall()
            checklists = []
            for row in rows:
                checklist = dict(row)
                try:
                    checklist['items'] = json.loads(checklist['items'])
                except (json.JSONDecodeError, TypeError):
                    checklist['items'] = []
                checklists.append(checklist)
            return {"checklists": checklists}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in list_move_checklists: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in list_move_checklists: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/tenancy/move-checklist/{checklist_id}")
def get_move_checklist(checklist_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Get a single checklist with items parsed from JSON."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM move_checklists WHERE id = ? AND household_id = ?",
                (checklist_id, household_id)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Checklist not found")
            checklist = dict(row)
            try:
                checklist['items'] = json.loads(checklist['items'])
            except (json.JSONDecodeError, TypeError):
                checklist['items'] = []
            return {"checklist": checklist}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_move_checklist: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_move_checklist: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/tenancy/move-checklist/{checklist_id}")
async def update_move_checklist(checklist_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update checklist items (toggle checked, add notes)."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        items = data.get('items')
        if items is None or not isinstance(items, list):
            logger.warning("Validation failed in update_move_checklist: %s", "Items array is required")
            raise HTTPException(status_code=400, detail="Items array is required")
        # --- End validation ---

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM move_checklists WHERE id = ? AND household_id = ?",
                (checklist_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Checklist not found")

            all_checked = all(item.get('checked', False) for item in items)
            status = 'completed' if all_checked else 'in_progress'
            completed_at = datetime.now().isoformat() if all_checked and existing['status'] != 'completed' else existing['completed_at']

            items_json = json.dumps(items)
            conn.execute("""
                UPDATE move_checklists SET items=?, status=?, completed_at=?
                WHERE id=? AND household_id=?
            """, (
                items_json,
                status,
                completed_at,
                checklist_id,
                household_id
            ))
            conn.commit()

        await manager.broadcast({"type": "tenancy_updated"}, household_id=household_id)
        return {"message": "Checklist updated", "status": status}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_move_checklist: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_move_checklist: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/tenancy/{tenancy_id}")
async def update_tenancy(tenancy_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update lease details. Manager only."""
    household_id = tenant.household_id
    if tenant.role != "manager":
        raise HTTPException(status_code=403, detail="Manager only")
    try:
        data = await request.json()

        # --- Input validation ---
        if 'address' in data:
            address = data.get('address', '').strip() if isinstance(data.get('address'), str) else ''
            if len(address) > 500:
                logger.warning("Validation failed in update_tenancy: %s", "Address is too long (max 500 characters)")
                raise HTTPException(status_code=400, detail="Address is too long (max 500 characters)")
            data['address'] = address

        if 'landlord_name' in data:
            landlord_name = data.get('landlord_name', '').strip() if isinstance(data.get('landlord_name'), str) else ''
            if len(landlord_name) > 200:
                logger.warning("Validation failed in update_tenancy: %s", "Landlord name is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Landlord name is too long (max 200 characters)")
            data['landlord_name'] = landlord_name

        if 'landlord_contact' in data:
            landlord_contact = data.get('landlord_contact', '').strip() if isinstance(data.get('landlord_contact'), str) else ''
            if len(landlord_contact) > 200:
                logger.warning("Validation failed in update_tenancy: %s", "Landlord contact is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Landlord contact is too long (max 200 characters)")
            data['landlord_contact'] = landlord_contact

        if 'real_estate_agent' in data:
            real_estate_agent = data.get('real_estate_agent', '').strip() if isinstance(data.get('real_estate_agent'), str) else ''
            if len(real_estate_agent) > 200:
                logger.warning("Validation failed in update_tenancy: %s", "Real estate agent name is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Real estate agent name is too long (max 200 characters)")
            data['real_estate_agent'] = real_estate_agent

        if 'agent_contact' in data:
            agent_contact = data.get('agent_contact', '').strip() if isinstance(data.get('agent_contact'), str) else ''
            if len(agent_contact) > 200:
                logger.warning("Validation failed in update_tenancy: %s", "Agent contact is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Agent contact is too long (max 200 characters)")
            data['agent_contact'] = agent_contact

        if 'bond_reference' in data:
            bond_reference = data.get('bond_reference', '').strip() if isinstance(data.get('bond_reference'), str) else ''
            if len(bond_reference) > 100:
                logger.warning("Validation failed in update_tenancy: %s", "Bond reference is too long (max 100 characters)")
                raise HTTPException(status_code=400, detail="Bond reference is too long (max 100 characters)")
            data['bond_reference'] = bond_reference

        if 'notes' in data:
            notes = data.get('notes', '').strip() if isinstance(data.get('notes'), str) else ''
            if len(notes) > 2000:
                logger.warning("Validation failed in update_tenancy: %s", "Notes are too long (max 2000 characters)")
                raise HTTPException(status_code=400, detail="Notes are too long (max 2000 characters)")
            data['notes'] = notes

        if 'rent_amount' in data and data['rent_amount'] is not None:
            try:
                rent_amount = float(data['rent_amount'])
                if rent_amount < 0 or rent_amount > 999999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in update_tenancy: %s", "Rent amount must be a valid number (0 to 999999999)")
                raise HTTPException(status_code=400, detail="Rent amount must be a valid number (0 to 999999999)")
            data['rent_amount'] = rent_amount

        if 'rent_frequency' in data:
            if data['rent_frequency'] not in VALID_RENT_FREQUENCIES:
                logger.warning("Validation failed in update_tenancy: %s", "Rent frequency must be weekly, fortnightly, or monthly")
                raise HTTPException(status_code=400, detail="Rent frequency must be weekly, fortnightly, or monthly")

        if 'bond_total' in data and data['bond_total'] is not None:
            try:
                bond_total = float(data['bond_total'])
                if bond_total < 0 or bond_total > 999999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in update_tenancy: %s", "Bond total must be a valid number (0 to 999999999)")
                raise HTTPException(status_code=400, detail="Bond total must be a valid number (0 to 999999999)")
            data['bond_total'] = bond_total

        if 'bond_lodged_with' in data:
            if data['bond_lodged_with'] not in VALID_BOND_LODGED:
                logger.warning("Validation failed in update_tenancy: %s", "Bond lodged with must be fair_trading, landlord, agent, or other")
                raise HTTPException(status_code=400, detail="Bond lodged with must be fair_trading, landlord, agent, or other")
        # --- End validation ---

        now = datetime.now().isoformat()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM tenancy WHERE id = ? AND household_id = ?",
                (tenancy_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Tenancy not found")

            conn.execute("""
                UPDATE tenancy SET address=?, landlord_name=?, landlord_contact=?,
                real_estate_agent=?, agent_contact=?, lease_start=?, lease_end=?,
                rent_amount=?, rent_frequency=?, bond_total=?, bond_lodged_with=?,
                bond_reference=?, notes=?, updated_at=?
                WHERE id=? AND household_id=?
            """, (
                data.get('address', existing['address']),
                data.get('landlord_name', existing['landlord_name']),
                data.get('landlord_contact', existing['landlord_contact']),
                data.get('real_estate_agent', existing['real_estate_agent']),
                data.get('agent_contact', existing['agent_contact']),
                data.get('lease_start', existing['lease_start']),
                data.get('lease_end', existing['lease_end']),
                data.get('rent_amount', existing['rent_amount']),
                data.get('rent_frequency', existing['rent_frequency']),
                data.get('bond_total', existing['bond_total']),
                data.get('bond_lodged_with', existing['bond_lodged_with']),
                data.get('bond_reference', existing['bond_reference']),
                data.get('notes', existing['notes']),
                now,
                tenancy_id,
                household_id
            ))
            conn.commit()

        await manager.broadcast({"type": "tenancy_updated"}, household_id=household_id)
        return {"message": "Tenancy updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_tenancy: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_tenancy: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Vehicles ───────────────────────────────────────────────────────────────────

@router.get("/api/vehicles")
def get_vehicles(tenant: TenantContext = Depends(get_current_user)):
    """List all vehicles with their maintenance items."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            vehicles = conn.execute(
                "SELECT * FROM vehicles WHERE household_id = ? ORDER BY name",
                (household_id,)
            ).fetchall()
            result = []
            for v in vehicles:
                vd = dict(v)
                maintenance = conn.execute(
                    "SELECT * FROM vehicle_maintenance WHERE vehicle_id = ? ORDER BY completed ASC, due_date ASC",
                    (v["id"],)
                ).fetchall()
                vd["maintenance"] = [dict(m) for m in maintenance]
                # Include recent charging sessions for EVs
                if vd.get("is_ev"):
                    sessions = conn.execute(
                        "SELECT * FROM ev_charging_sessions WHERE vehicle_id = ? ORDER BY charge_date DESC LIMIT 20",
                        (v["id"],)
                    ).fetchall()
                    vd["charging_sessions"] = [dict(s) for s in sessions]
                result.append(vd)
            return {"vehicles": result}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_vehicles: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_vehicles: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/vehicles")
async def create_vehicle(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Add a vehicle."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Vehicle name is required")
        with get_db() as conn:
            conn.execute(
                """INSERT INTO vehicles (name, make, model, year, rego_plate, rego_expiry,
                   insurance_provider, insurance_expiry, odometer, notes, is_ev, battery_kwh, household_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    data.get("make"),
                    data.get("model"),
                    data.get("year"),
                    data.get("rego_plate"),
                    data.get("rego_expiry"),
                    data.get("insurance_provider"),
                    data.get("insurance_expiry"),
                    data.get("odometer"),
                    data.get("notes"),
                    1 if data.get("is_ev") else 0,
                    data.get("battery_kwh"),
                    household_id,
                ),
            )
            conn.commit()
        await manager.broadcast({"type": "vehicles_updated"}, household_id=household_id)
        return {"ok": True}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_vehicle: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_vehicle: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/vehicles/{vehicle_id}")
async def delete_vehicle(vehicle_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a vehicle and its maintenance records."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM vehicles WHERE id = ? AND household_id = ?",
                (vehicle_id, household_id)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Vehicle not found")
            conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
            conn.commit()
        await manager.broadcast({"type": "vehicles_updated"}, household_id=household_id)
        return {"ok": True}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_vehicle: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_vehicle: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/vehicles/{vehicle_id}/maintenance")
async def add_maintenance(vehicle_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Add a maintenance item to a vehicle."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        title = (data.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title is required")
        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM vehicles WHERE id = ? AND household_id = ?",
                (vehicle_id, household_id)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Vehicle not found")
            conn.execute(
                """INSERT INTO vehicle_maintenance (vehicle_id, title, description, due_date,
                   due_odometer, cost, service_type, recurrence_km, recurrence_months, household_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    vehicle_id,
                    title,
                    data.get("description"),
                    data.get("due_date"),
                    data.get("due_odometer"),
                    data.get("cost"),
                    data.get("service_type"),
                    data.get("recurrence_km"),
                    data.get("recurrence_months"),
                    household_id,
                ),
            )
            conn.commit()
        await manager.broadcast({"type": "vehicles_updated"}, household_id=household_id)
        return {"ok": True}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in add_maintenance: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in add_maintenance: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/vehicles/maintenance/{maintenance_id}/complete")
async def complete_maintenance(maintenance_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Toggle maintenance item completion. Optionally accepts odometer reading and auto-creates next service if recurring."""
    household_id = tenant.household_id
    try:
        data = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            data = await request.json()
        odometer_reading = data.get("odometer")

        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM vehicle_maintenance WHERE id = ? AND household_id = ?",
                (maintenance_id, household_id)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Maintenance item not found")
            new_status = 0 if row["completed"] else 1
            now_iso = datetime.now().isoformat()

            update_fields = ["completed = ?", "completed_at = ?", "completed_by = ?"]
            update_params = [new_status, now_iso if new_status else None, tenant.display_name if new_status else None]

            if odometer_reading and new_status:
                update_fields.append("odometer_at_completion = ?")
                update_params.append(int(odometer_reading))
                # Also update the vehicle's current odometer
                conn.execute(
                    "UPDATE vehicles SET odometer = ? WHERE id = ? AND household_id = ?",
                    (int(odometer_reading), row["vehicle_id"], household_id),
                )

            update_params.append(maintenance_id)
            update_params.append(household_id)
            conn.execute(
                f"UPDATE vehicle_maintenance SET {', '.join(update_fields)} WHERE id = ? AND household_id = ?",
                update_params,
            )

            # Auto-create next service if this is a recurring item being completed
            if new_status and (row["recurrence_km"] or row["recurrence_months"]):
                from dateutil.relativedelta import relativedelta
                next_due_date = None
                next_due_odo = None
                if row["recurrence_months"]:
                    base_date = date.today()
                    next_due_date = (base_date + relativedelta(months=row["recurrence_months"])).isoformat()
                if row["recurrence_km"]:
                    base_odo = int(odometer_reading) if odometer_reading else (row["due_odometer"] or 0)
                    next_due_odo = base_odo + row["recurrence_km"]
                conn.execute(
                    """INSERT INTO vehicle_maintenance
                       (vehicle_id, title, description, due_date, due_odometer, service_type,
                        recurrence_km, recurrence_months, household_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["vehicle_id"], row["title"], row["description"],
                        next_due_date, next_due_odo, row["service_type"],
                        row["recurrence_km"], row["recurrence_months"], household_id,
                    ),
                )

            conn.commit()
        await manager.broadcast({"type": "vehicles_updated"}, household_id=household_id)
        return {"ok": True}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in complete_maintenance: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in complete_maintenance: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/vehicles/maintenance/{maintenance_id}")
async def delete_maintenance(maintenance_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a maintenance item."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM vehicle_maintenance WHERE id = ? AND household_id = ?",
                (maintenance_id, household_id)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Maintenance item not found")
            conn.execute("DELETE FROM vehicle_maintenance WHERE id = ? AND household_id = ?", (maintenance_id, household_id))
            conn.commit()
        await manager.broadcast({"type": "vehicles_updated"}, household_id=household_id)
        return {"ok": True}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_maintenance: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_maintenance: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# --- EV Charging Session Endpoints ---

@router.post("/api/vehicles/{vehicle_id}/charging")
async def log_charging_session(vehicle_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Log an EV charging session."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        with get_db() as conn:
            vehicle = conn.execute(
                "SELECT id, is_ev FROM vehicles WHERE id = ? AND household_id = ?",
                (vehicle_id, household_id)
            ).fetchone()
            if not vehicle:
                raise HTTPException(status_code=404, detail="Vehicle not found")
            if not vehicle["is_ev"]:
                raise HTTPException(status_code=400, detail="Vehicle is not marked as EV")

            charge_date = data.get("charge_date") or datetime.now().strftime("%Y-%m-%d")
            conn.execute(
                """INSERT INTO ev_charging_sessions
                   (vehicle_id, charge_date, start_percent, end_percent, kwh_added, cost,
                    location, charger_type, notes, logged_by, household_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    vehicle_id,
                    charge_date,
                    data.get("start_percent"),
                    data.get("end_percent"),
                    data.get("kwh_added"),
                    data.get("cost"),
                    data.get("location", "Home"),
                    data.get("charger_type", "home"),
                    data.get("notes"),
                    data.get("logged_by") or tenant.display_name,
                    household_id,
                ),
            )
            conn.commit()
        await manager.broadcast({"type": "vehicles_updated"}, household_id=household_id)
        return {"ok": True, "message": "Charging session logged"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in log_charging_session: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in log_charging_session: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/vehicles/{vehicle_id}/charging")
def get_charging_sessions(vehicle_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Get all charging sessions for a vehicle with summary stats."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            vehicle = conn.execute(
                "SELECT id, is_ev, battery_kwh FROM vehicles WHERE id = ? AND household_id = ?",
                (vehicle_id, household_id)
            ).fetchone()
            if not vehicle:
                raise HTTPException(status_code=404, detail="Vehicle not found")

            sessions = conn.execute(
                "SELECT * FROM ev_charging_sessions WHERE vehicle_id = ? ORDER BY charge_date DESC",
                (vehicle_id,)
            ).fetchall()
            session_list = [dict(s) for s in sessions]

            # Calculate stats
            total_kwh = sum(s.get("kwh_added") or 0 for s in session_list)
            total_cost = sum(s.get("cost") or 0 for s in session_list)
            session_count = len(session_list)

            # This month's stats
            now = datetime.now()
            month_prefix = now.strftime("%Y-%m")
            month_sessions = [s for s in session_list if s.get("charge_date", "").startswith(month_prefix)]
            month_kwh = sum(s.get("kwh_added") or 0 for s in month_sessions)
            month_cost = sum(s.get("cost") or 0 for s in month_sessions)

            return {
                "sessions": session_list,
                "stats": {
                    "total_sessions": session_count,
                    "total_kwh": round(total_kwh, 1),
                    "total_cost": round(total_cost, 2),
                    "month_sessions": len(month_sessions),
                    "month_kwh": round(month_kwh, 1),
                    "month_cost": round(month_cost, 2),
                    "avg_kwh_per_session": round(total_kwh / session_count, 1) if session_count else 0,
                }
            }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_charging_sessions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_charging_sessions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/vehicles/charging/{session_id}")
async def delete_charging_session(session_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a charging session."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute(
                "DELETE FROM ev_charging_sessions WHERE id = ? AND household_id = ?",
                (session_id, household_id)
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Session not found")
            conn.commit()
        await manager.broadcast({"type": "vehicles_updated"}, household_id=household_id)
        return {"ok": True}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_charging_session: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_charging_session: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
