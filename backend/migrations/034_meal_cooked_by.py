"""Add cooked_by column to meals table for per-meal cook assignment."""

import sqlite3


def up(conn: sqlite3.Connection):
    conn.execute("ALTER TABLE meals ADD COLUMN cooked_by TEXT")
