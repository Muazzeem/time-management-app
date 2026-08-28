"""One-time script: copy the local time_log.db into a Turso database.

Run this after creating your Turso database, before (or instead of)
letting the app auto-seed demo data on Turso. It copies your real
projects, entries, and hourly rate across, preserving IDs so foreign
keys stay correct.

Usage:
    TURSO_DATABASE_URL=libsql://your-db.turso.io \\
    TURSO_AUTH_TOKEN=your-token \\
    python migrate_to_turso.py
"""

import os
import sqlite3
import sys
from pathlib import Path

import turso_serverless

DB_PATH = Path(__file__).parent / "time_log.db"


def main():
    url = os.environ.get("TURSO_DATABASE_URL")
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if not url or not token:
        sys.exit("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN environment variables first.")

    if not DB_PATH.exists():
        sys.exit(f"No local database found at {DB_PATH}")

    local = sqlite3.connect(DB_PATH)
    local.row_factory = sqlite3.Row

    remote = turso_serverless.connect(url, auth_token=token)
    remote.row_factory = turso_serverless.Row

    remote.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    remote.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            task TEXT NOT NULL,
            hours REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Not Started'
        )
        """
    )
    remote.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            hourly_rate REAL NOT NULL DEFAULT 300
        )
        """
    )
    remote.commit()

    existing_projects = remote.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
    if existing_projects:
        sys.exit(
            "The Turso database already has project(s) in it. Refusing to overwrite —\n"
            "empty it first (or use a fresh database) if you want to re-run this."
        )

    projects = local.execute("SELECT * FROM projects").fetchall()
    for p in projects:
        remote.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
            (p["id"], p["name"], p["created_at"]),
        )

    entries = local.execute("SELECT * FROM entries").fetchall()
    for e in entries:
        remote.execute(
            "INSERT INTO entries (id, project_id, date, task, hours, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (e["id"], e["project_id"], e["date"], e["task"], e["hours"], e["status"]),
        )

    rate_row = local.execute("SELECT hourly_rate FROM settings WHERE id = 1").fetchone()
    rate = rate_row["hourly_rate"] if rate_row else 300.0
    remote.execute(
        "INSERT INTO settings (id, hourly_rate) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET hourly_rate = excluded.hourly_rate",
        (rate,),
    )
    remote.commit()

    print(f"Migrated {len(projects)} project(s), {len(entries)} entr(y/ies), rate ৳{rate:.2f}/hr.")


if __name__ == "__main__":
    main()
