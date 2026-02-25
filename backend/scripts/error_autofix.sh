#!/bin/bash
# Daily error log auto-fix using Claude Code
# Scans huddle.log for recent errors and launches Claude to diagnose/fix.
# Crontab: 0 19 * * * /home/keiran/huddle/backend/scripts/error_autofix.sh

set -euo pipefail

PROJECT_DIR="/home/keiran/huddle/backend"
LOG_FILE="$PROJECT_DIR/logs/huddle.log"
AUTOFIX_LOG="$PROJECT_DIR/logs/autofix.log"
LOCK_FILE="/tmp/huddle-autofix.lock"

# Prevent concurrent runs
if [ -f "$LOCK_FILE" ]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null)
    if kill -0 "$pid" 2>/dev/null; then
        echo "$(date -Iseconds) Already running (PID $pid), skipping" >> "$AUTOFIX_LOG"
        exit 0
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

echo "$(date -Iseconds) Starting error log scan..." >> "$AUTOFIX_LOG"

# Extract unique ERROR/CRITICAL lines from the last 24 hours
ERRORS=$(grep -E "ERROR|CRITICAL" "$LOG_FILE" 2>/dev/null | \
    grep "$(date -d '1 day ago' '+%Y-%m-%d')\|$(date '+%Y-%m-%d')" | \
    grep -v "Push notification failed.*410 Gone" | \
    sort -u | tail -30)

if [ -z "$ERRORS" ]; then
    echo "$(date -Iseconds) No actionable errors found. Clean!" >> "$AUTOFIX_LOG"
    exit 0
fi

ERROR_COUNT=$(echo "$ERRORS" | wc -l)
echo "$(date -Iseconds) Found $ERROR_COUNT unique errors, launching Claude..." >> "$AUTOFIX_LOG"

# Build the prompt
PROMPT="You are doing automated daily maintenance on the Huddle app at /home/keiran/huddle/backend.

The following errors appeared in the log in the last 24 hours:

\`\`\`
$ERRORS
\`\`\`

For each error:
1. Read the relevant source file and understand the root cause
2. Fix the bug if it's a code issue (not a transient network error)
3. After all fixes, restart the service: sudo systemctl restart chores-kiosk.service
4. Commit any changes with a descriptive message
5. For each error you fix, mark it as rectified by calling:
   curl -s -X POST http://localhost:8001/api/status/rectify \\
     -H 'Content-Type: application/json' \\
     -d '{\"source\": \"<the error source e.g. calendar.get_family_calendar_month>\", \"pattern\": \"<key part of error message>\"}'

Rules:
- Only fix actual code bugs, not transient issues (network timeouts, temporary API failures)
- Do NOT fix push 410 Gone errors (already handled with auto-cleanup)
- Test that the service starts cleanly after your changes
- If no code fixes are needed, just exit cleanly
- Always call the rectify endpoint for errors you fix so they show as resolved on the status page"

cd "$PROJECT_DIR"

# Run Claude in non-interactive mode with full permissions
/home/keiran/.local/bin/claude --dangerously-skip-permissions -p "$PROMPT" \
    >> "$AUTOFIX_LOG" 2>&1 || true

echo "$(date -Iseconds) Auto-fix session complete" >> "$AUTOFIX_LOG"
echo "---" >> "$AUTOFIX_LOG"
