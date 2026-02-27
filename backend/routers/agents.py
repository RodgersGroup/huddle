"""API router for agent actions — audit trail and status."""
import logging
import sqlite3
from fastapi import APIRouter, Depends, HTTPException
from auth import get_current_user, TenantContext
from db import get_db

logger = logging.getLogger("huddle")
router = APIRouter()


@router.get("/api/agents/actions")
def get_actions(
    limit: int = 50,
    severity: str = None,
    tenant: TenantContext = Depends(get_current_user),
):
    """Get recent agent actions (audit trail)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            if severity:
                rows = conn.execute(
                    "SELECT * FROM agent_actions WHERE (household_id=? OR household_id IS NULL) AND severity=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (household_id, severity, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM agent_actions WHERE (household_id=? OR household_id IS NULL) "
                    "ORDER BY created_at DESC LIMIT ?",
                    (household_id, limit),
                ).fetchall()
        return {"actions": [dict(r) for r in rows]}
    except sqlite3.Error as e:
        logger.error("Database error in get_actions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/api/agents/health")
def get_agent_health(tenant: TenantContext = Depends(get_current_user)):
    """Get agent health summary — counts by severity and auto-fix rate."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            stats = conn.execute("""
                SELECT severity, auto_fixed, COUNT(*) as cnt
                FROM agent_actions
                WHERE (household_id = ? OR household_id IS NULL)
                  AND created_at > datetime('now', '-1 day')
                GROUP BY severity, auto_fixed
            """, (household_id,)).fetchall()

            critical = conn.execute("""
                SELECT title, detail, created_at, auto_fixed FROM agent_actions
                WHERE (household_id = ? OR household_id IS NULL) AND severity = 'critical'
                ORDER BY created_at DESC LIMIT 5
            """, (household_id,)).fetchall()

        summary = {"info": 0, "warning": 0, "critical": 0, "auto_fixed": 0, "total": 0}
        for row in stats:
            summary[row["severity"]] = summary.get(row["severity"], 0) + row["cnt"]
            if row["auto_fixed"]:
                summary["auto_fixed"] += row["cnt"]
            summary["total"] += row["cnt"]

        return {
            "last_24h": summary,
            "recent_critical": [dict(r) for r in critical],
        }
    except sqlite3.Error as e:
        logger.error("Database error in get_agent_health: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
