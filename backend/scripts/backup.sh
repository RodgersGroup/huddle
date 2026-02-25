#!/bin/bash
# Huddle Database Backup Script
# Uses Python's sqlite3.backup() for WAL-safe backups
#
# Add to crontab with: crontab -e
# 0 */6 * * * /home/keiran/huddle/backend/scripts/backup.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Source .env for config values (e.g. BACKUP_RETENTION_DAYS)
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

DB_FILE="$PROJECT_DIR/chores.db"
BACKUP_DIR="$PROJECT_DIR/backups"
LOG_FILE="$PROJECT_DIR/logs/backup.log"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="huddle_backup_${TIMESTAMP}.db"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"
MIN_KEEP=7
MAX_AGE_DAYS="${BACKUP_RETENTION_DAYS:-30}"

# Ensure directories exist
mkdir -p "$BACKUP_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "$LOG_FILE"
}

log "INFO  | Starting backup"

# Check source database exists
if [ ! -f "$DB_FILE" ]; then
    log "ERROR | Database not found: $DB_FILE"
    exit 1
fi

# Perform WAL-safe backup using Python's sqlite3.backup()
if python3 -c "
import sqlite3, sys
try:
    src = sqlite3.connect('$DB_FILE')
    dst = sqlite3.connect('$BACKUP_PATH')
    src.backup(dst)
    dst.close()
    src.close()
except Exception as e:
    print(f'Backup failed: {e}', file=sys.stderr)
    sys.exit(1)
"; then
    log "INFO  | Database backed up to $BACKUP_NAME"
else
    log "ERROR | Python backup failed"
    exit 1
fi

# Compress with gzip
if gzip "$BACKUP_PATH"; then
    COMPRESSED_SIZE=$(du -h "${BACKUP_PATH}.gz" | cut -f1)
    log "INFO  | Compressed to ${BACKUP_NAME}.gz ($COMPRESSED_SIZE)"
else
    log "ERROR | Compression failed"
    exit 1
fi

# Clean up old backups (keep at least MIN_KEEP, delete older than MAX_AGE_DAYS)
TOTAL_BACKUPS=$(find "$BACKUP_DIR" -name "huddle_backup_*.db.gz" | wc -l)

if [ "$TOTAL_BACKUPS" -gt "$MIN_KEEP" ]; then
    DELETED=0
    while IFS= read -r old_backup; do
        REMAINING=$(find "$BACKUP_DIR" -name "huddle_backup_*.db.gz" | wc -l)
        if [ "$REMAINING" -le "$MIN_KEEP" ]; then
            break
        fi
        rm -f "$old_backup"
        DELETED=$((DELETED + 1))
        log "INFO  | Deleted old backup: $(basename "$old_backup")"
    done < <(find "$BACKUP_DIR" -name "huddle_backup_*.db.gz" -mtime +${MAX_AGE_DAYS} | sort)

    if [ "$DELETED" -gt 0 ]; then
        log "INFO  | Cleaned up $DELETED old backup(s)"
    fi
fi

FINAL_COUNT=$(find "$BACKUP_DIR" -name "huddle_backup_*.db.gz" | wc -l)
log "INFO  | Backup complete. Total backups: $FINAL_COUNT"
