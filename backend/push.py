"""
Push notification functionality for Huddle.
Handles VAPID key loading, sending push notifications, and push API endpoints.
"""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext

from db import get_db
from settings import get_people

logger = logging.getLogger("huddle")

# Push notification support
try:
    from pywebpush import webpush, WebPushException
    PUSH_ENABLED = True
except ImportError:
    PUSH_ENABLED = False
    logger.warning("pywebpush not installed - push notifications disabled")

# VAPID keys for push notifications
# TODO: Move VAPID keys to environment variables (VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY)
#       instead of reading from a JSON file. The file approach works but env vars are
#       more standard for secrets management and easier to rotate in production.
BASE_DIR = Path(__file__).parent
VAPID_KEYS_FILE = BASE_DIR / "vapid_keys.json"
VAPID_PRIVATE_KEY = None
VAPID_PUBLIC_KEY = None
VAPID_CLAIMS = {"sub": "mailto:noreply@huddle.rodgersgroup.au"}
try:
    with open(VAPID_KEYS_FILE) as f:
        _vapid = json.load(f)
        VAPID_PRIVATE_KEY = _vapid["private_key"]
        VAPID_PUBLIC_KEY = _vapid["public_key"]
    logger.info("VAPID keys loaded from %s", VAPID_KEYS_FILE)
except FileNotFoundError:
    logger.info("VAPID keys file not found at %s - push notifications disabled", VAPID_KEYS_FILE)
except (json.JSONDecodeError, KeyError) as e:
    logger.error("VAPID keys file is corrupted or missing required keys: %s", e)
except Exception as e:
    logger.error("Failed to load VAPID keys: %s", e, exc_info=True)


def send_push_notification(subscription_info: dict, title: str, body: str, tag: str = "chores", module: str = "", actions: list = None, renotify: bool = False):
    """Send a push notification to a single subscription.

    Args:
        actions: List of action dicts, e.g. [{"action": "mark_done", "title": "Mark Done"}]
        renotify: If True, replacement notifications (same tag) still vibrate/sound
    """
    if not PUSH_ENABLED or not VAPID_PRIVATE_KEY:
        return False
    try:
        endpoint = subscription_info.get("endpoint", "")
        parsed = urlparse(endpoint)
        aud = f"{parsed.scheme}://{parsed.netloc}"
        claims = {**VAPID_CLAIMS, "aud": aud}
        payload = {"title": title, "body": body, "tag": tag}
        if module:
            payload["module"] = module
        if actions:
            payload["actions"] = actions
        if renotify:
            payload["renotify"] = True
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=claims
        )
        return True
    except WebPushException as e:
        resp = getattr(e, 'response', None)
        status = getattr(resp, 'status_code', None) or getattr(resp, 'status', None)
        # Fallback: parse status from message like "Push failed: 410 Gone"
        if status is None:
            import re
            m = re.search(r'\b(4\d\d|5\d\d)\b', str(e))
            if m:
                status = int(m.group(1))
        if status in (410, 404):
            logger.info("Push subscription expired (HTTP %s), will remove", status)
            return "expired"
        logger.error("Push notification failed (HTTP %s): %s", status, e)
        return False
    except Exception as e:
        logger.error("Push notification error: %s", e, exc_info=True)
        return False


def send_push_to_person(person: str, title: str, body: str, tag: str = "chores", *, household_id: int, module: str = "", actions: list = None, renotify: bool = False):
    """Send push notification to all subscriptions for a person."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, subscription FROM push_subscriptions WHERE person = ? AND household_id = ?",
            (person, household_id)
        ).fetchall()

        expired_ids = []
        for row in rows:
            try:
                sub_info = json.loads(row["subscription"])
                result = send_push_notification(sub_info, title, body, tag, module=module, actions=actions, renotify=renotify)
                if result == "expired":
                    expired_ids.append(row["id"])
            except Exception as e:
                logger.error("Error sending push to %s: %s", person, e, exc_info=True)

        # Clean up expired subscriptions
        for sub_id in expired_ids:
            conn.execute("DELETE FROM push_subscriptions WHERE id = ? AND household_id = ?", (sub_id, household_id))
        if expired_ids:
            conn.commit()
            logger.info("Removed %d expired subscriptions for %s", len(expired_ids), person)


def send_push_to_person_bg(person: str, title: str, body: str, tag: str = "chores", *, household_id: int, module: str = "", actions: list = None, renotify: bool = False):
    """Fire-and-forget push notification in a daemon thread. Use from request handlers."""
    import threading
    t = threading.Thread(
        target=send_push_to_person,
        args=(person, title, body, tag),
        kwargs=dict(household_id=household_id, module=module, actions=actions, renotify=renotify),
        daemon=True,
    )
    t.start()


async def send_push_to_all(title: str, body: str, tag: str = "chores", *, household_id: int, module: str = ""):
    """Send push notification to all people in a household."""
    # get_people() performs a synchronous DB call — run in a thread to avoid
    # blocking the event loop (see M3/M8 audit).
    people = await asyncio.to_thread(get_people, household_id=household_id)
    for person in people:
        await asyncio.to_thread(
            send_push_to_person, person, title, body, tag,
            household_id=household_id, module=module
        )


router = APIRouter()


@router.get("/api/push/vapid-key")
def get_vapid_key():
    """Return the public VAPID key for push subscription."""
    try:
        if not VAPID_PUBLIC_KEY:
            raise HTTPException(status_code=503, detail="Push notifications not configured")
        return {"public_key": VAPID_PUBLIC_KEY}
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in get_vapid_key: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/push/subscribe")
async def push_subscribe(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Save a push subscription for a person."""
    household_id = tenant.household_id

    try:
        data = await request.json()
        person = data.get("person")
        subscription = data.get("subscription")

        if not person or person not in get_people(household_id=household_id):
            raise HTTPException(status_code=400, detail="Invalid person")
        if not subscription:
            raise HTTPException(status_code=400, detail="Subscription data required")

        sub_json = json.dumps(subscription, sort_keys=True)

        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO push_subscriptions (person, subscription, household_id) VALUES (?, ?, ?)",
                (person, sub_json, household_id)
            )
            conn.commit()

        return {"message": f"Push subscription saved for {person}"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in push_subscribe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in push_subscribe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/push/subscribe")
async def push_unsubscribe(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Remove a push subscription."""
    household_id = tenant.household_id

    try:
        data = await request.json()
        person = data.get("person")
        subscription = data.get("subscription")

        if not person or not subscription:
            raise HTTPException(status_code=400, detail="Person and subscription required")

        sub_json = json.dumps(subscription, sort_keys=True)

        with get_db() as conn:
            conn.execute(
                "DELETE FROM push_subscriptions WHERE person = ? AND subscription = ? AND household_id = ?",
                (person, sub_json, household_id)
            )
            conn.commit()

        return {"message": "Subscription removed"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in push_unsubscribe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in push_unsubscribe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/push/test/{person}")
async def push_test(person: str, tenant: TenantContext = Depends(get_current_user)):
    """Send a test push notification to a person."""
    household_id = tenant.household_id

    try:
        if person not in get_people(household_id=household_id):
            raise HTTPException(status_code=400, detail="Invalid person")

        await asyncio.to_thread(
            send_push_to_person, person, "Huddle: Test Notification",
            f"Hi {person}! Push notifications are working.", "test",
            household_id=household_id
        )
        return {"message": f"Test notification sent to {person}"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in push_test: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in push_test: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
