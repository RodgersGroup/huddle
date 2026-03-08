#!/usr/bin/env python3
"""
Huddle Tunnel Health Check — verifies external access via Cloudflare Tunnel.

Checks that huddle.rodgersgroup.au is reachable from the server's perspective,
the SSL cert is valid, and the response comes from the actual app (not an error page).
Logs results and sends a push notification if the tunnel is down.

Crontab: */15 * * * * /home/keiran/huddle/backend/venv/bin/python /home/keiran/huddle/backend/scripts/tunnel_health.py
"""

import json
import logging
import logging.handlers
import os
import ssl
import socket
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATE_FILE = LOG_DIR / "tunnel_state.json"

EXTERNAL_URL = "https://huddle.rodgersgroup.au/api/health/ping"
EXTERNAL_HOST = "huddle.rodgersgroup.au"
CERT_WARN_DAYS = 14  # Warn if cert expires within this many days

# Logger
logger = logging.getLogger("huddle.tunnel")
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "tunnel.log",
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(formatter)
logger.addHandler(console)


def _load_state():
    """Load previous tunnel state."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"status": "unknown", "last_check": None, "down_since": None, "notified": False, "alert_attempts": 0}


def _save_state(state):
    """Persist tunnel state."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.error("Failed to save state: %s", e)


NTFY_TOPIC = "keiran-server"


def _send_ntfy(title, body, priority="high"):
    """Send notification via ntfy.sh (works even when web push can't)."""
    try:
        data = body.encode("utf-8")
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=data,
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": "rotating_light" if priority == "urgent" else "warning",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logger.info("ntfy alert sent: %s", title)
                return True
    except Exception as e:
        logger.warning("ntfy alert failed: %s", e)
    return False


def _send_alert(title, body):
    """Send notification via ntfy and web push."""
    # Always try ntfy first (most reliable)
    ntfy_sent = _send_ntfy(title, body)

    # Also try web push for in-app notifications
    push_sent = _send_push_alert(title, body)

    return ntfy_sent or push_sent


def _send_push_alert(title, body):
    """Send push notification to superadmins only about tunnel issue."""
    try:
        # Use the app's own push infrastructure via local API
        import sqlite3
        db_file = PROJECT_DIR / "chores.db"
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row

        # Get superadmin emails from config
        sys.path.insert(0, str(PROJECT_DIR))
        from config import SUPERADMIN_EMAILS

        if not SUPERADMIN_EMAILS:
            # Fallback: use household_id=1 manager (original admin)
            admins = conn.execute("""
                SELECT hm.display_name, hm.household_id
                FROM household_members hm
                WHERE hm.household_id = 1 AND hm.role = 'manager'
                LIMIT 1
            """).fetchall()
        else:
            # Find household members matching superadmin emails
            placeholders = ",".join("?" * len(SUPERADMIN_EMAILS))
            admins = conn.execute(f"""
                SELECT hm.display_name, hm.household_id
                FROM household_members hm
                JOIN users u ON u.id = hm.user_id
                WHERE u.email IN ({placeholders})
            """, SUPERADMIN_EMAILS).fetchall()

        if not admins:
            conn.close()
            return

        # Load VAPID keys
        vapid_file = PROJECT_DIR / "vapid_keys.json"
        if not vapid_file.exists():
            conn.close()
            return

        with open(vapid_file) as f:
            vapid = json.load(f)
        private_key = vapid["private_key"]

        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            conn.close()
            return

        from urllib.parse import urlparse

        sent = 0
        for mgr in admins:
            subs = conn.execute(
                "SELECT subscription FROM push_subscriptions WHERE person = ? AND household_id = ?",
                (mgr["display_name"], mgr["household_id"]),
            ).fetchall()

            for sub_row in subs:
                try:
                    sub_info = json.loads(sub_row["subscription"])
                    endpoint = sub_info.get("endpoint", "")
                    parsed = urlparse(endpoint)
                    aud = f"{parsed.scheme}://{parsed.netloc}"
                    webpush(
                        subscription_info=sub_info,
                        data=json.dumps({"title": title, "body": body, "tag": "tunnel-alert"}),
                        vapid_private_key=private_key,
                        vapid_claims={"sub": "mailto:chores@example.com", "aud": aud},
                    )
                    sent += 1
                except Exception:
                    pass

        conn.close()
        if sent:
            logger.info("Sent %d alert notification(s)", sent)
            return True
        else:
            logger.warning("No push notifications were delivered (network may be down)")
            return False
    except Exception as e:
        logger.error("Failed to send alert: %s", e)
        return False


def check_ssl_cert():
    """Check SSL certificate expiry."""
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=EXTERNAL_HOST) as s:
            s.settimeout(10)
            s.connect((EXTERNAL_HOST, 443))
            cert = s.getpeercert()

        # Parse expiry
        not_after = cert.get("notAfter", "")
        # Format: 'Jan  1 00:00:00 2025 GMT'
        expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
        from datetime import timezone
        days_left = (expiry - datetime.now(timezone.utc).replace(tzinfo=None)).days

        if days_left < CERT_WARN_DAYS:
            logger.warning("SSL cert expires in %d days (on %s)", days_left, not_after)
            return "warning", days_left
        else:
            logger.info("SSL cert valid, expires in %d days", days_left)
            return "ok", days_left
    except Exception as e:
        logger.error("SSL cert check failed: %s", e)
        return "error", -1


def check_tunnel():
    """Check if the external URL is reachable and returns valid response."""
    try:
        req = urllib.request.Request(EXTERNAL_URL, headers={
            "User-Agent": "HuddleTunnelCheck/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8", errors="replace")
                if '"ok"' in body or '"status"' in body:
                    logger.info("Tunnel: reachable (200 OK, valid response)")
                    return True
                else:
                    logger.warning("Tunnel: reachable but unexpected response: %s", body[:200])
                    return True  # Still reachable, just unexpected content
            else:
                logger.error("Tunnel: HTTP %d", resp.status)
                return False
    except urllib.error.HTTPError as e:
        # Cloudflare Access might return 403 — that still means tunnel works
        if e.code in (401, 403):
            logger.info("Tunnel: reachable (HTTP %d — likely Cloudflare Access)", e.code)
            return True
        logger.error("Tunnel: HTTP error %d", e.code)
        return False
    except Exception as e:
        logger.error("Tunnel: unreachable (%s)", e)
        return False


def main():
    logger.info("--- Tunnel health check ---")
    state = _load_state()
    now = datetime.now().isoformat()

    # Check SSL cert
    cert_status, cert_days = check_ssl_cert()
    if cert_status == "warning" and not state.get("cert_warned"):
        _send_alert(
            "SSL Certificate Expiring",
            f"The SSL cert for {EXTERNAL_HOST} expires in {cert_days} days. Cloudflare should auto-renew.",
        )
        state["cert_warned"] = True

    # Check tunnel reachability
    tunnel_ok = check_tunnel()

    state["last_check"] = now

    if tunnel_ok:
        if state.get("status") == "down":
            # Recovery — tunnel was down, now back up
            down_since = state.get("down_since", "unknown")
            logger.info("Tunnel recovered (was down since %s)", down_since)
            _send_alert("Tunnel Recovered", f"huddle.rodgersgroup.au is back online (was down since {down_since})")
        state["status"] = "up"
        state["down_since"] = None
        state["notified"] = False
        state["alert_attempts"] = 0
    else:
        if state.get("status") != "down":
            state["down_since"] = now
        state["status"] = "down"

        # Alert on first detection of downtime, with max 3 retry attempts
        # (prevents infinite retry spam when network is fully down)
        attempts = state.get("alert_attempts", 0)
        if not state.get("notified") and attempts < 3:
            logger.warning("Tunnel is DOWN — sending alert (attempt %d/3)", attempts + 1)
            alert_sent = _send_alert(
                "Tunnel Down",
                f"huddle.rodgersgroup.au is unreachable. Check Cloudflare Tunnel status.",
            )
            state["alert_attempts"] = attempts + 1
            if alert_sent:
                state["notified"] = True
            else:
                logger.warning("Alert failed to send (network likely down) — will retry next check")

    _save_state(state)
    logger.info("--- Check complete (status: %s) ---", state["status"])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            logger.critical("Tunnel check fatal error: %s", e, exc_info=True)
        except Exception:
            print(f"TUNNEL CHECK FATAL: {e}", file=sys.stderr)
