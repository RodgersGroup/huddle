import json
import logging
import random
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, require_role, TenantContext
from datetime import datetime, date
from zoneinfo import ZoneInfo
from db import get_db
from settings import get_setting
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()


def _today_local(household_id: int) -> date:
    """Get today's date in the household's local timezone."""
    tz = ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))
    return datetime.now(tz).date()


def _is_task_due_today(task: dict, today: date) -> bool:
    """Determine if a pet task is due today based on its schedule."""
    schedule_type = task.get('schedule_type', 'daily')
    if schedule_type == 'daily':
        return True
    if schedule_type == 'days':
        try:
            schedule_days = json.loads(task.get('schedule_days') or '[]')
        except (json.JSONDecodeError, TypeError):
            schedule_days = []
        day_name = today.strftime('%A')
        return day_name in schedule_days
    return False


@router.get("/api/pets")
def get_pets(tenant: TenantContext = Depends(get_current_user)):
    """List all pets with task counts and upcoming vet appointments."""
    household_id = tenant.household_id
    try:
        today = _today_local(household_id)
        today_str = today.isoformat()
        day_name = today.strftime('%A')

        with get_db() as conn:
            pets = conn.execute(
                "SELECT * FROM pets WHERE household_id = ? AND deleted_at IS NULL ORDER BY name",
                (household_id,)
            ).fetchall()

            result = []
            for pet in pets:
                pet_dict = dict(pet)
                pet_id = pet_dict['id']

                tasks = conn.execute(
                    "SELECT * FROM pet_tasks WHERE pet_id = ? AND household_id = ?",
                    (pet_id, household_id)
                ).fetchall()

                tasks_due_today = 0
                for task in tasks:
                    task_dict = dict(task)
                    if _is_task_due_today(task_dict, today):
                        tasks_due_today += 1

                upcoming_vet = conn.execute(
                    "SELECT * FROM pet_vet_appointments WHERE pet_id = ? AND household_id = ? AND date >= ? AND completed = 0 ORDER BY date, time LIMIT 3",
                    (pet_id, household_id, today_str)
                ).fetchall()

                pet_dict['task_count'] = len(tasks)
                pet_dict['tasks_due_today'] = tasks_due_today
                pet_dict['upcoming_vet_appointments'] = [dict(a) for a in upcoming_vet]
                result.append(pet_dict)

            return {"pets": result}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_pets: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_pets: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/pets")
async def create_pet(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Create a new pet."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
        if not name:
            logger.warning("Validation failed in create_pet: %s", "Name is required")
            raise HTTPException(status_code=400, detail="Name is required")
        if len(name) > 100:
            logger.warning("Validation failed in create_pet: %s", "Name is too long (max 100 characters)")
            raise HTTPException(status_code=400, detail="Name is too long (max 100 characters)")

        species = data.get('species', '').strip() if isinstance(data.get('species'), str) else ''
        if not species:
            logger.warning("Validation failed in create_pet: %s", "Species is required")
            raise HTTPException(status_code=400, detail="Species is required")
        if len(species) > 50:
            logger.warning("Validation failed in create_pet: %s", "Species is too long (max 50 characters)")
            raise HTTPException(status_code=400, detail="Species is too long (max 50 characters)")

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO pets (name, species, breed, date_of_birth, icon, notes, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                species,
                data.get('breed', '').strip() if isinstance(data.get('breed'), str) else '',
                data.get('date_of_birth'),
                data.get('icon', '').strip() if isinstance(data.get('icon'), str) else '',
                data.get('notes', '').strip() if isinstance(data.get('notes'), str) else '',
                household_id
            ))
            conn.commit()
            pet_id = cursor.lastrowid

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"id": pet_id, "message": "Pet created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_pet: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_pet: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/pets/{pet_id}")
async def update_pet(pet_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Update a pet."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        if 'name' in data:
            name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
            if not name:
                logger.warning("Validation failed in update_pet: %s", "Name cannot be empty")
                raise HTTPException(status_code=400, detail="Name cannot be empty")
            if len(name) > 100:
                logger.warning("Validation failed in update_pet: %s", "Name is too long (max 100 characters)")
                raise HTTPException(status_code=400, detail="Name is too long (max 100 characters)")
            data['name'] = name

        if 'species' in data:
            species = data.get('species', '').strip() if isinstance(data.get('species'), str) else ''
            if not species:
                logger.warning("Validation failed in update_pet: %s", "Species cannot be empty")
                raise HTTPException(status_code=400, detail="Species cannot be empty")
            if len(species) > 50:
                logger.warning("Validation failed in update_pet: %s", "Species is too long (max 50 characters)")
                raise HTTPException(status_code=400, detail="Species is too long (max 50 characters)")
            data['species'] = species

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM pets WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (pet_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Pet not found")

            conn.execute("""
                UPDATE pets SET name=?, species=?, breed=?, date_of_birth=?, icon=?, notes=?
                WHERE id=? AND household_id=?
            """, (
                data.get('name', existing['name']),
                data.get('species', existing['species']),
                data.get('breed', existing['breed']),
                data.get('date_of_birth', existing['date_of_birth']),
                data.get('icon', existing['icon']),
                data.get('notes', existing['notes']),
                pet_id,
                household_id
            ))
            conn.commit()

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"message": "Pet updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_pet: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_pet: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/pets/{pet_id}")
async def delete_pet(pet_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Delete a pet and cascade to its tasks, completions, and vet appointments."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM pets WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (pet_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Pet not found")

            task_ids = conn.execute(
                "SELECT id FROM pet_tasks WHERE pet_id = ? AND household_id = ?",
                (pet_id, household_id)
            ).fetchall()
            for task_row in task_ids:
                conn.execute(
                    "DELETE FROM pet_task_completions WHERE task_id = ? AND household_id = ?",
                    (task_row['id'], household_id)
                )

            conn.execute(
                "DELETE FROM pet_tasks WHERE pet_id = ? AND household_id = ?",
                (pet_id, household_id)
            )
            conn.execute(
                "DELETE FROM pet_vet_appointments WHERE pet_id = ? AND household_id = ?",
                (pet_id, household_id)
            )
            conn.execute(
                "UPDATE pets SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?",
                (pet_id, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"message": "Pet deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_pet: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_pet: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/pets/{pet_id}/tasks")
def get_pet_tasks(pet_id: int, tenant: TenantContext = Depends(get_current_user)):
    """List tasks for a pet with today's completion status."""
    household_id = tenant.household_id
    try:
        today = _today_local(household_id)
        today_str = today.isoformat()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM pets WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (pet_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Pet not found")

            tasks = conn.execute(
                "SELECT * FROM pet_tasks WHERE pet_id = ? AND household_id = ? ORDER BY name",
                (pet_id, household_id)
            ).fetchall()

            result = []
            for task in tasks:
                task_dict = dict(task)
                task_dict['due_today'] = _is_task_due_today(task_dict, today)

                completions_today = conn.execute(
                    "SELECT * FROM pet_task_completions WHERE task_id = ? AND household_id = ? AND DATE(completed_at) = ?",
                    (task_dict['id'], household_id, today_str)
                ).fetchall()
                task_dict['completions_today'] = [dict(c) for c in completions_today]
                task_dict['completed_today'] = len(completions_today) > 0

                try:
                    task_dict['people'] = json.loads(task_dict.get('people') or '[]')
                except (json.JSONDecodeError, TypeError):
                    task_dict['people'] = []

                try:
                    task_dict['schedule_days'] = json.loads(task_dict.get('schedule_days') or '[]')
                except (json.JSONDecodeError, TypeError):
                    task_dict['schedule_days'] = []

                try:
                    task_dict['schedule_times'] = json.loads(task_dict.get('schedule_times') or '[]')
                except (json.JSONDecodeError, TypeError):
                    task_dict['schedule_times'] = []

                result.append(task_dict)

            return {"tasks": result}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_pet_tasks: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_pet_tasks: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/pets/{pet_id}/tasks")
async def create_pet_task(pet_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Create a task for a pet."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM pets WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (pet_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Pet not found")

        name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
        if not name:
            logger.warning("Validation failed in create_pet_task: %s", "Name is required")
            raise HTTPException(status_code=400, detail="Name is required")
        if len(name) > 200:
            logger.warning("Validation failed in create_pet_task: %s", "Name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")

        people = data.get('people', [])
        if not isinstance(people, list):
            people = []
        people_json = json.dumps(people)

        schedule_days = data.get('schedule_days', [])
        if not isinstance(schedule_days, list):
            schedule_days = []
        schedule_days_json = json.dumps(schedule_days)

        schedule_times = data.get('schedule_times', [])
        if not isinstance(schedule_times, list):
            schedule_times = []
        schedule_times_json = json.dumps(schedule_times)

        with get_db() as conn:
            # Stagger starting index across existing pet tasks to avoid clustering
            if people:
                existing = conn.execute(
                    "SELECT current_person_index FROM pet_tasks WHERE household_id = ? AND people = ?",
                    (household_id, people_json)
                ).fetchall()
                used_indexes = [r[0] % len(people) for r in existing]
                index_counts = {i: used_indexes.count(i) for i in range(len(people))}
                min_count = min(index_counts.values()) if index_counts else 0
                least_used = [i for i, c in index_counts.items() if c == min_count]
                start_idx = random.choice(least_used)
            else:
                start_idx = 0

            cursor = conn.execute("""
                INSERT INTO pet_tasks (pet_id, name, task_type, schedule_type, schedule_days, schedule_times, people, current_person_index, notes, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pet_id,
                name,
                data.get('task_type', 'care'),
                data.get('schedule_type', 'daily'),
                schedule_days_json,
                schedule_times_json,
                people_json,
                start_idx,
                data.get('notes', '').strip() if isinstance(data.get('notes'), str) else '',
                household_id
            ))
            conn.commit()
            task_id = cursor.lastrowid

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"id": task_id, "message": "Task created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_pet_task: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_pet_task: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/pets/tasks/{task_id}")
async def update_pet_task(task_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Update a pet task."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        if 'name' in data:
            name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
            if not name:
                logger.warning("Validation failed in update_pet_task: %s", "Name cannot be empty")
                raise HTTPException(status_code=400, detail="Name cannot be empty")
            if len(name) > 200:
                logger.warning("Validation failed in update_pet_task: %s", "Name is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")
            data['name'] = name

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM pet_tasks WHERE id = ? AND household_id = ?",
                (task_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Task not found")

            people = data.get('people')
            if people is not None:
                if not isinstance(people, list):
                    people = []
                people_json = json.dumps(people)
            else:
                people_json = existing['people'] or '[]'

            schedule_days = data.get('schedule_days')
            if schedule_days is not None:
                if not isinstance(schedule_days, list):
                    schedule_days = []
                schedule_days_json = json.dumps(schedule_days)
            else:
                schedule_days_json = existing['schedule_days'] or '[]'

            schedule_times = data.get('schedule_times')
            if schedule_times is not None:
                if not isinstance(schedule_times, list):
                    schedule_times = []
                schedule_times_json = json.dumps(schedule_times)
            else:
                schedule_times_json = existing['schedule_times'] or '[]'

            conn.execute("""
                UPDATE pet_tasks SET name=?, task_type=?, schedule_type=?, schedule_days=?, schedule_times=?, people=?, current_person_index=?, notes=?
                WHERE id=? AND household_id=?
            """, (
                data.get('name', existing['name']),
                data.get('task_type', existing['task_type']),
                data.get('schedule_type', existing['schedule_type']),
                schedule_days_json,
                schedule_times_json,
                people_json,
                data.get('current_person_index', existing['current_person_index']),
                data.get('notes', existing['notes']),
                task_id,
                household_id
            ))
            conn.commit()

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"message": "Task updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_pet_task: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_pet_task: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/pets/tasks/{task_id}")
async def delete_pet_task(task_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Delete a pet task and its completions."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM pet_tasks WHERE id = ? AND household_id = ?",
                (task_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Task not found")

            conn.execute(
                "DELETE FROM pet_task_completions WHERE task_id = ? AND household_id = ?",
                (task_id, household_id)
            )
            conn.execute(
                "DELETE FROM pet_tasks WHERE id = ? AND household_id = ?",
                (task_id, household_id)
            )
            conn.commit()

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"message": "Task deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_pet_task: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_pet_task: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/pets/tasks/{task_id}/complete")
async def complete_pet_task(task_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Record a task completion, preventing duplicates by the same person today."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        completed_by = data.get('completed_by', '').strip() if isinstance(data.get('completed_by'), str) else ''
        if not completed_by:
            completed_by = tenant.display_name

        today = _today_local(household_id)
        today_str = today.isoformat()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM pet_tasks WHERE id = ? AND household_id = ?",
                (task_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Task not found")

            already_done = conn.execute(
                "SELECT id FROM pet_task_completions WHERE task_id = ? AND completed_by = ? AND DATE(completed_at) = ? AND household_id = ?",
                (task_id, completed_by, today_str, household_id)
            ).fetchone()
            if already_done:
                raise HTTPException(status_code=409, detail="Task already completed by this person today")

            now = datetime.now(ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))).replace(tzinfo=None).isoformat()

            cursor = conn.execute("""
                INSERT INTO pet_task_completions (task_id, completed_by, scheduled_time, completed_at, household_id)
                VALUES (?, ?, ?, ?, ?)
            """, (
                task_id,
                completed_by,
                data.get('scheduled_time'),
                now,
                household_id
            ))

            # Advance rotation to next person
            people = json.loads(existing['people']) if existing['people'] else []
            if people and len(people) > 1:
                current_idx = existing['current_person_index']
                new_idx = (current_idx + 1) % len(people)
                if people[new_idx] == completed_by:
                    new_idx = (new_idx + 1) % len(people)
                conn.execute(
                    "UPDATE pet_tasks SET current_person_index = ? WHERE id = ? AND household_id = ?",
                    (new_idx, task_id, household_id)
                )

            conn.commit()
            completion_id = cursor.lastrowid

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"id": completion_id, "message": "Task completed"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in complete_pet_task: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in complete_pet_task: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/pets/{pet_id}/vet")
def get_vet_appointments(pet_id: int, tenant: TenantContext = Depends(get_current_user)):
    """List vet appointments for a pet."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM pets WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (pet_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Pet not found")

            appointments = conn.execute(
                "SELECT * FROM pet_vet_appointments WHERE pet_id = ? AND household_id = ? ORDER BY date, time",
                (pet_id, household_id)
            ).fetchall()

            return {"appointments": [dict(a) for a in appointments]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_vet_appointments: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_vet_appointments: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/pets/{pet_id}/vet")
async def create_vet_appointment(pet_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Create a vet appointment for a pet."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM pets WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (pet_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Pet not found")

        title = data.get('title', '').strip() if isinstance(data.get('title'), str) else ''
        if not title:
            logger.warning("Validation failed in create_vet_appointment: %s", "Title is required")
            raise HTTPException(status_code=400, detail="Title is required")
        if len(title) > 200:
            logger.warning("Validation failed in create_vet_appointment: %s", "Title is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Title is too long (max 200 characters)")

        if not data.get('date'):
            logger.warning("Validation failed in create_vet_appointment: %s", "Date is required")
            raise HTTPException(status_code=400, detail="Date is required")

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO pet_vet_appointments (pet_id, title, date, time, vet_name, notes, completed, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pet_id,
                title,
                data['date'],
                data.get('time'),
                data.get('vet_name', '').strip() if isinstance(data.get('vet_name'), str) else '',
                data.get('notes', '').strip() if isinstance(data.get('notes'), str) else '',
                0,
                household_id
            ))
            conn.commit()
            appt_id = cursor.lastrowid

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"id": appt_id, "message": "Appointment created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_vet_appointment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_vet_appointment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/pets/vet/{appt_id}")
async def update_vet_appointment(appt_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Update a vet appointment."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        if 'title' in data:
            title = data.get('title', '').strip() if isinstance(data.get('title'), str) else ''
            if not title:
                logger.warning("Validation failed in update_vet_appointment: %s", "Title cannot be empty")
                raise HTTPException(status_code=400, detail="Title cannot be empty")
            if len(title) > 200:
                logger.warning("Validation failed in update_vet_appointment: %s", "Title is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Title is too long (max 200 characters)")
            data['title'] = title

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM pet_vet_appointments WHERE id = ? AND household_id = ?",
                (appt_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Appointment not found")

            conn.execute("""
                UPDATE pet_vet_appointments SET title=?, date=?, time=?, vet_name=?, notes=?, completed=?
                WHERE id=? AND household_id=?
            """, (
                data.get('title', existing['title']),
                data.get('date', existing['date']),
                data.get('time', existing['time']),
                data.get('vet_name', existing['vet_name']),
                data.get('notes', existing['notes']),
                1 if data.get('completed', existing['completed']) else 0,
                appt_id,
                household_id
            ))
            conn.commit()

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"message": "Appointment updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_vet_appointment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_vet_appointment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/pets/vet/{appt_id}")
async def delete_vet_appointment(appt_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Delete a vet appointment."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute(
                "DELETE FROM pet_vet_appointments WHERE id = ? AND household_id = ?",
                (appt_id, household_id)
            )
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Appointment not found")

        await manager.broadcast({"type": "pets_updated"}, household_id=household_id)
        return {"message": "Appointment deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_vet_appointment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_vet_appointment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
