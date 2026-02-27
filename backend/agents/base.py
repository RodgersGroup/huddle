"""Shared utilities for self-healing agents."""
import logging
import time
from db import get_db
from push import send_push_to_person, PUSH_ENABLED, VAPID_PRIVATE_KEY

logger = logging.getLogger("huddle.agents")

# Track last run per (agent, check_name) to avoid running too frequently
_last_run: dict[tuple[str, str], float] = {}


def should_run(agent: str, check: str, interval_seconds: int) -> bool:
    """Throttle: returns True only if interval has elapsed since last run of this check."""
    key = (agent, check)
    now = time.time()
    if now - _last_run.get(key, 0) < interval_seconds:
        return False
    _last_run[key] = now
    return True


def log_action(
    agent: str,
    action_type: str,
    title: str,
    detail: str = "",
    severity: str = "info",
    auto_fixed: bool = False,
    household_id: int = None,
):
    """Record an agent action in the audit trail and log it."""
    level = {"info": logging.INFO, "warning": logging.WARNING, "critical": logging.CRITICAL}.get(severity, logging.INFO)
    logger.log(level, "[%s] %s: %s %s", agent, action_type, title, "(auto-fixed)" if auto_fixed else "")

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO agent_actions (agent, action_type, severity, title, detail, auto_fixed, household_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent, action_type, severity, title, detail, 1 if auto_fixed else 0, household_id),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Failed to record agent action: %s", e)


def alert_managers(title: str, body: str, household_id: int = None):
    """Send push notification to all managers (or all people if no role distinction).
    Used for issues that can't be auto-fixed."""
    if not PUSH_ENABLED or not VAPID_PRIVATE_KEY:
        return
    try:
        from settings import get_people
        if household_id:
            people = get_people(household_id=household_id)
            for person in people:
                send_push_to_person(
                    person, f"Huddle Agent: {title}", body,
                    "agent-alert", household_id=household_id, module="agents",
                )
        else:
            # System-wide alert: send to all households
            from background import _get_all_household_ids
            for hid in _get_all_household_ids():
                people = get_people(household_id=hid)
                for person in people[:1]:  # Just first person per household for system alerts
                    send_push_to_person(
                        person, f"Huddle Agent: {title}", body,
                        "agent-alert", household_id=hid, module="agents",
                    )
    except Exception as e:
        logger.warning("Failed to send agent alert: %s", e)
