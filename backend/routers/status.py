import json
import logging
import os
import shutil
import subprocess
import sys
import sqlite3
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from db import get_db

logger = logging.getLogger("huddle")

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent
LOG_FILE = BASE_DIR / "logs" / "huddle.log"
RECTIFIED_FILE = BASE_DIR / "logs" / "rectified_errors.json"
DB_FILE = BASE_DIR / "chores.db"
UTC = ZoneInfo("UTC")


def _load_rectified():
    """Load set of rectified error signatures from JSON file."""
    try:
        if RECTIFIED_FILE.exists():
            with open(RECTIFIED_FILE) as f:
                data = json.load(f)
            return {(e["source"], e["pattern"]) for e in data if "source" in e and "pattern" in e}
    except Exception:
        pass
    return set()


def _local_tz():
    # Status page is system-wide (no household context), use default timezone
    return ZoneInfo("Australia/Sydney")


def _get_health():
    """Gather system health info."""
    health = {}

    # Server uptime
    try:
        with open("/proc/uptime") as f:
            uptime_secs = float(f.read().split()[0])
        days, rem = divmod(int(uptime_secs), 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        health["uptime"] = f"{days}d {hours}h {mins}m"
        health["uptime_seconds"] = int(uptime_secs)
    except Exception:
        health["uptime"] = "unknown"

    # Python version
    health["python"] = sys.version.split()[0]

    # Database
    try:
        with get_db() as conn:
            conn.execute("SELECT 1").fetchone()
        health["database"] = "ok"
    except Exception as e:
        health["database"] = f"error: {e}"

    # Database file size
    try:
        size_bytes = DB_FILE.stat().st_size
        if size_bytes > 1_048_576:
            health["db_size"] = f"{size_bytes / 1_048_576:.1f} MB"
        else:
            health["db_size"] = f"{size_bytes / 1024:.0f} KB"
    except Exception:
        health["db_size"] = "unknown"

    # Disk usage
    try:
        usage = shutil.disk_usage("/")
        used_pct = (usage.used / usage.total) * 100
        free_gb = usage.free / (1024 ** 3)
        health["disk_used"] = f"{used_pct:.1f}%"
        health["disk_free"] = f"{free_gb:.1f} GB"
    except Exception:
        health["disk_used"] = "unknown"
        health["disk_free"] = "unknown"

    # Memory
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    meminfo[parts[0].strip()] = parts[1].strip()
        avail_kb = int(meminfo.get("MemAvailable", "0 kB").split()[0])
        total_kb = int(meminfo.get("MemTotal", "0 kB").split()[0])
        health["mem_available"] = f"{avail_kb // 1024} MB"
        health["mem_total"] = f"{total_kb // 1024} MB"
        health["mem_used_pct"] = f"{((total_kb - avail_kb) / total_kb) * 100:.0f}%" if total_kb else "unknown"
    except Exception:
        health["mem_available"] = "unknown"

    # Timestamp
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Sydney")
        health["server_time"] = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        health["server_time"] = datetime.now().isoformat()

    return health


def _get_errors(limit: int = 50):
    """Read recent ERROR/CRITICAL lines from the log file."""
    errors = deque(maxlen=limit)
    rectified = _load_rectified()
    try:
        if not LOG_FILE.exists():
            return []
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip()
                if " | ERROR" in line or " | CRITICAL" in line:
                    parts = line.split(" | ", 3)
                    if len(parts) >= 4:
                        # Convert UTC log timestamp to local timezone
                        ts = parts[0].strip()
                        try:
                            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                            dt = dt.replace(tzinfo=UTC).astimezone(_local_tz())
                            ts = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass
                        source = parts[2].strip()
                        message = parts[3].strip()
                        # Check if this error matches a rectified pattern
                        is_rectified = any(
                            source == src and pattern in message
                            for src, pattern in rectified
                        )
                        errors.append({
                            "timestamp": ts,
                            "level": parts[1].strip(),
                            "source": source,
                            "message": message,
                            "rectified": is_rectified,
                        })
                    else:
                        errors.append({
                            "timestamp": "",
                            "level": "ERROR",
                            "source": "",
                            "message": line,
                            "rectified": False,
                        })
    except Exception as e:
        logger.warning("Failed to read log file: %s", e)
        return []
    return list(reversed(errors))


def _get_changelog(limit: int = 30):
    """Read recent git commits."""
    try:
        # Use ASCII record/unit separators to safely parse multi-line bodies
        sep = "\x1e"  # record separator (between fields)
        end = "\x1f"  # unit separator (between commits)
        result = subprocess.run(
            ["git", "log", f"--format=%H{sep}%s{sep}%b{sep}%ai{end}", f"-{limit}"],
            capture_output=True, text=True, timeout=5,
            cwd=str(BASE_DIR),
        )
        if result.returncode != 0:
            return []
        commits = []
        for record in result.stdout.split(end):
            record = record.strip()
            if not record:
                continue
            parts = record.split(sep)
            if len(parts) < 4:
                continue
            hash_full, subject, body, date_str = parts[0], parts[1], parts[2], parts[3]
            subject = subject.strip()
            # Clean up body: strip Co-Authored-By and trailing whitespace
            body_lines = [l for l in body.strip().splitlines()
                          if not l.strip().startswith("Co-Authored-By:")]
            description = "\n".join(body_lines).strip()
            # Categorise by first word
            first_word = subject.split()[0].lower().rstrip(":") if subject else ""
            if first_word in ("fix", "fixed", "bugfix"):
                category = "fix"
            elif first_word in ("add", "added", "create", "implement"):
                category = "feature"
            elif first_word in ("update", "bump", "upgrade"):
                category = "update"
            elif first_word in ("remove", "delete", "drop"):
                category = "remove"
            elif first_word in ("refactor", "simplify", "clean"):
                category = "refactor"
            elif first_word in ("roll", "make"):
                category = "update"
            elif first_word in ("prevent",):
                category = "fix"
            else:
                category = "change"
            # Parse date and convert to household timezone
            try:
                dt = datetime.fromisoformat(date_str.strip())
                dt = dt.astimezone(_local_tz())
                date_display = dt.strftime("%Y-%m-%d %H:%M")
                date_group = dt.strftime("%Y-%m-%d")
            except Exception:
                date_display = date_str.strip()
                date_group = date_str.strip()[:10]
            commits.append({
                "hash": hash_full[:7],
                "hash_full": hash_full,
                "subject": subject,
                "description": description,
                "date": date_display,
                "date_group": date_group,
                "category": category,
            })
        return commits
    except Exception as e:
        logger.warning("Failed to read git log: %s", e)
        return []


OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5-coder:7b"
OLLAMA_TIMEOUT = 60  # seconds — LLM on CPU is slow
OLLAMA_BATCH_SIZE = 3  # small batches = faster generation per call


def _ollama_rewrite(subjects: list[str]) -> list[str]:
    """Call Ollama to rewrite a batch of commit subjects. Returns list of summaries or empty on failure."""
    numbered = "\n".join(f'{i + 1}. "{s}"' for i, s in enumerate(subjects))
    prompt = (
        "Rewrite these git commit messages into plain English for non-technical users of a household management app. "
        "Each summary should be one short, friendly sentence describing what changed from the user's perspective. "
        "Do not mention code, functions, variables, or technical terms. "
        "Return ONLY a numbered list with the rewritten messages, nothing else.\n\n"
        f"{numbered}"
    )
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        result = json.loads(resp.read().decode())

    text = result.get("response", "")
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    summaries = []
    for line in lines:
        stripped = line.lstrip("0123456789").lstrip(".):- ").strip()
        if stripped.startswith('"') and stripped.endswith('"'):
            stripped = stripped[1:-1]
        if stripped:
            summaries.append(stripped)
    return summaries


def _load_cached_summaries(hashes: list[str]) -> dict[str, str]:
    """Load existing summaries from DB cache."""
    if not hashes:
        return {}
    try:
        with get_db() as conn:
            placeholders = ",".join("?" for _ in hashes)
            rows = conn.execute(
                f"SELECT hash, summary FROM changelog_summaries WHERE hash IN ({placeholders})",
                hashes,
            ).fetchall()
            return {r["hash"]: r["summary"] for r in rows}
    except Exception as e:
        logger.warning("Failed to read changelog cache: %s", e)
        return {}


def _attach_summaries(commits: list) -> list:
    """Attach cached summaries to commits. Does NOT call Ollama — cache-only, instant."""
    if not commits:
        return commits
    cached = _load_cached_summaries([c["hash"] for c in commits])
    for c in commits:
        if c["hash"] in cached:
            c["summary"] = cached[c["hash"]]
    return commits


def seed_summaries(commits: list) -> int:
    """Generate and cache summaries for all uncached commits. Calls Ollama synchronously.
    Returns the number of new summaries generated. Used by the seeding script."""
    if not commits:
        return 0
    cached = _load_cached_summaries([c["hash"] for c in commits])
    uncached = [c for c in commits if c["hash"] not in cached]
    if not uncached:
        return 0

    generated = 0
    for batch_start in range(0, len(uncached), OLLAMA_BATCH_SIZE):
        batch = uncached[batch_start:batch_start + OLLAMA_BATCH_SIZE]
        try:
            subjects = [c["subject"] for c in batch]
            summaries = _ollama_rewrite(subjects)
            if summaries:
                with get_db() as conn:
                    for i, c in enumerate(batch):
                        if i < len(summaries):
                            c["summary"] = summaries[i]
                            conn.execute(
                                "INSERT OR IGNORE INTO changelog_summaries (hash, summary) VALUES (?, ?)",
                                (c["hash"], summaries[i]),
                            )
                    conn.commit()
                generated += min(len(summaries), len(batch))
        except Exception as e:
            logger.warning("Ollama failed for batch at offset %d: %s", batch_start, e)
            break  # Stop if Ollama is down
    return generated


@router.post("/api/status/rectify")
async def rectify_error(request: Request):
    """Mark an error pattern as rectified. Used by autofix script."""
    try:
        body = await request.json()
        source = body.get("source", "").strip()
        pattern = body.get("pattern", "").strip()
        if not source or not pattern:
            raise HTTPException(status_code=400, detail="source and pattern required")

        # Load existing, append, save
        entries = []
        try:
            if RECTIFIED_FILE.exists():
                with open(RECTIFIED_FILE) as f:
                    entries = json.load(f)
        except Exception:
            entries = []

        # Avoid duplicates
        if not any(e.get("source") == source and e.get("pattern") == pattern for e in entries):
            entries.append({
                "source": source,
                "pattern": pattern,
                "fixed_at": datetime.now().isoformat(),
            })
            with open(RECTIFIED_FILE, "w") as f:
                json.dump(entries, f, indent=2)

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in rectify_error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/status")
def get_status():
    """System status, error logs, and changelog. No auth — protected by Cloudflare Access externally."""
    try:
        changelog = _get_changelog(30)
        _attach_summaries(changelog)
        return JSONResponse(
            content={
                "health": _get_health(),
                "errors": _get_errors(50),
                "changelog": changelog,
            },
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in get_status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
