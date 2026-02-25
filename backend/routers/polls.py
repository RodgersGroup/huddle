import logging
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, require_role, TenantContext
from datetime import datetime
from db import get_db
from settings import get_people
from websocket import manager
from push import send_push_to_person_bg

logger = logging.getLogger("huddle")

router = APIRouter()


@router.get("/api/polls")
def get_polls(tenant: TenantContext = Depends(get_current_user)):
    """Get all polls with vote counts and voter details."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            polls = conn.execute("SELECT * FROM polls WHERE household_id = ? ORDER BY created_at DESC", (household_id,)).fetchall()
            result = []
            for poll in polls:
                poll_dict = dict(poll)
                options = conn.execute(
                    "SELECT * FROM poll_options WHERE poll_id = ? ORDER BY id", (poll['id'],)
                ).fetchall()
                poll_dict['options'] = []
                total_votes = 0
                for opt in options:
                    opt_dict = dict(opt)
                    votes = conn.execute(
                        "SELECT voter FROM poll_votes WHERE option_id = ?", (opt['id'],)
                    ).fetchall()
                    opt_dict['votes'] = [v['voter'] for v in votes]
                    opt_dict['vote_count'] = len(opt_dict['votes'])
                    total_votes += opt_dict['vote_count']
                    poll_dict['options'].append(opt_dict)
                poll_dict['total_votes'] = total_votes
                result.append(poll_dict)
            return {"polls": result}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_polls: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_polls: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/polls")
async def create_poll(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Create a new poll with options."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        question = data.get('question', '').strip() if isinstance(data.get('question'), str) else ''
        options = data.get('options', [])
        created_by = data.get('created_by')
        if not question or len(options) < 2:
            logger.warning("Validation failed in create_poll: %s", "Question and at least 2 options required")
            raise HTTPException(status_code=400, detail="Question and at least 2 options required")
        if len(question) > 200:
            logger.warning("Validation failed in create_poll: %s", "Question is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Question is too long (max 200 characters)")
        data['question'] = question
        if not isinstance(options, list):
            logger.warning("Validation failed in create_poll: %s", "options must be a list")
            raise HTTPException(status_code=400, detail="options must be a list")
        for i, opt in enumerate(options):
            if isinstance(opt, str) and len(opt) > 200:
                logger.warning("Validation failed in create_poll: %s", f"Option text is too long at index {i} (max 200 characters)")
                raise HTTPException(status_code=400, detail=f"Option text is too long at index {i} (max 200 characters)")
        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO polls (question, created_by, poll_type, closes_at, household_id)
                VALUES (?, ?, ?, ?, ?)
            """, (question, created_by, data.get('poll_type', 'single'), data.get('closes_at'), household_id))
            poll_id = cursor.lastrowid
            for opt_text in options:
                conn.execute("INSERT INTO poll_options (poll_id, option_text) VALUES (?, ?)", (poll_id, opt_text))
            conn.commit()
        await manager.broadcast({"type": "poll_created", "poll_id": poll_id}, household_id=household_id)
        for person in get_people(household_id=household_id):
            if person != created_by:
                send_push_to_person_bg(person, "Huddle: New Poll", f"{created_by or 'Someone'} asks: {question}", "poll", household_id=household_id, module="polls")
        return {"id": poll_id, "message": "Poll created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_poll: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_poll: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/polls/{poll_id}/vote")
async def vote_poll(poll_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Cast or change a vote (upsert)."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        voter = data.get('voter', '').strip() if isinstance(data.get('voter'), str) else ''
        option_id = data.get('option_id')
        if not voter or not option_id:
            logger.warning("Validation failed in vote_poll: %s", "voter and option_id required")
            raise HTTPException(status_code=400, detail="voter and option_id required")
        if len(voter) > 200:
            logger.warning("Validation failed in vote_poll: %s", "voter name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="voter name is too long (max 200 characters)")
        if not isinstance(option_id, int):
            try:
                option_id = int(option_id)
            except (TypeError, ValueError):
                logger.warning("Validation failed in vote_poll: %s", "option_id must be an integer")
                raise HTTPException(status_code=400, detail="option_id must be an integer")
        data['voter'] = voter
        data['option_id'] = option_id
        with get_db() as conn:
            poll = conn.execute("SELECT * FROM polls WHERE id = ? AND household_id = ?", (poll_id, household_id)).fetchone()
            if not poll:
                raise HTTPException(status_code=404, detail="Poll not found")
            if poll['status'] != 'active':
                raise HTTPException(status_code=400, detail="Poll is closed")
            opt = conn.execute("SELECT * FROM poll_options WHERE id = ? AND poll_id = ?", (option_id, poll_id)).fetchone()
            if not opt:
                raise HTTPException(status_code=400, detail="Invalid option for this poll")
            existing = conn.execute("SELECT * FROM poll_votes WHERE poll_id = ? AND voter = ?", (poll_id, voter)).fetchone()
            if existing:
                conn.execute("UPDATE poll_votes SET option_id = ?, voted_at = ? WHERE id = ? AND poll_id = ?",
                             (option_id, datetime.now().isoformat(), existing['id'], poll_id))
            else:
                conn.execute("INSERT INTO poll_votes (poll_id, option_id, voter) VALUES (?, ?, ?)",
                             (poll_id, option_id, voter))
            conn.commit()
        await manager.broadcast({"type": "poll_voted", "poll_id": poll_id}, household_id=household_id)
        return {"message": "Vote recorded"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in vote_poll: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in vote_poll: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/polls/{poll_id}/close")
async def close_poll(poll_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Close a poll."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute("UPDATE polls SET status = 'closed' WHERE id = ? AND household_id = ?", (poll_id, household_id))
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Poll not found")
        await manager.broadcast({"type": "poll_closed", "poll_id": poll_id}, household_id=household_id)
        return {"message": "Poll closed"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in close_poll: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in close_poll: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/polls/{poll_id}")
async def delete_poll(poll_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Delete a poll and all its options/votes."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            # Verify ownership before deleting children
            poll = conn.execute("SELECT id FROM polls WHERE id = ? AND household_id = ?", (poll_id, household_id)).fetchone()
            if not poll:
                raise HTTPException(status_code=404, detail="Poll not found")
            conn.execute("DELETE FROM poll_votes WHERE poll_id = ? AND poll_id IN (SELECT id FROM polls WHERE household_id = ?)", (poll_id, household_id))
            conn.execute("DELETE FROM poll_options WHERE poll_id = ? AND poll_id IN (SELECT id FROM polls WHERE household_id = ?)", (poll_id, household_id))
            conn.execute("DELETE FROM polls WHERE id = ? AND household_id = ?", (poll_id, household_id))
            conn.commit()
        await manager.broadcast({"type": "poll_deleted", "poll_id": poll_id}, household_id=household_id)
        return {"message": "Poll deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_poll: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_poll: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
