"""Add photo_path column to adhoc_tasks for task photo attachments."""


def up(conn):
    # Add photo_path column to store the filename of the attached photo
    try:
        conn.execute("ALTER TABLE adhoc_tasks ADD COLUMN photo_path TEXT DEFAULT NULL")
    except Exception:
        pass  # Column may already exist
