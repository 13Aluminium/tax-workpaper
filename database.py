"""
database.py
-----------
Everything a HUMAN does in the workpaper is saved here, in a SQLite file.

Why SQLite?
  - It's a single file, no server to install. Perfect for a prototype.
  - It still gives us real SQL, transactions and constraints.
  - Moving to PostgreSQL later mostly means changing the connection code.

Design rule: we mostly INSERT and almost never DELETE or UPDATE in place.
For example, undoing an adjustment sets its status to 'reverted' instead of
deleting the row. That keeps a full history (who, what, when, why), which is
exactly what an auditor wants to see.

This is the "core demo" version: only the 6 tables the 5 tabs need.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import g

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "workpaper.db"))

SCHEMA = """
-- An adjustment = "the engine said X, we say Y, because Z".
-- target_type is 'line' (a return line like SchC.1) or 'household'
-- (a household field like theo.ssn). The original value is kept.
CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    version TEXT NOT NULL,            -- which engine version it was made against
    old_value TEXT,                   -- stored as JSON text so numbers and strings both work
    new_value TEXT,
    reason TEXT NOT NULL,
    category TEXT,                    -- tax-effect category (wages, schc_profit, ...)
    federal_effect REAL DEFAULT 0,    -- estimated change in federal tax
    california_effect REAL DEFAULT 0, -- estimated change in California tax
    status TEXT NOT NULL,             -- active | pending_partner | reverted
    issue_id TEXT,                    -- which check/issue this resolves (optional)
    batch_id TEXT,                    -- bulk actions share a batch id so they can be undone together
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT
);

-- Sign-offs. fingerprint = a hash of what was signed. If the thing changes
-- later, the fingerprint no longer matches and the sign-off shows as REOPENED.
CREATE TABLE IF NOT EXISTS signoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,        -- line | return
    target_id TEXT NOT NULL,
    level TEXT NOT NULL,              -- preparer | reviewer | partner
    fingerprint TEXT NOT NULL,
    batch_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active | undone
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Decisions on the automatic checks ("issues"): accepted as is, or dismissed, with a reason.
CREATE TABLE IF NOT EXISTS issue_status (
    issue_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,             -- open | accepted | dismissed
    note TEXT,
    updated_by TEXT,
    updated_at TEXT
);

-- Filing attempts. snapshot = a frozen copy of every value at the moment of filing.
CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,               -- original
    jurisdiction TEXT NOT NULL,       -- federal | both
    status TEXT NOT NULL,             -- rejected | accepted
    detail TEXT,
    snapshot TEXT,                    -- JSON of all effective values when filed
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Key/value settings. Here only the return status (in_preparation | filed).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The audit trail. Every write goes here: who did what, when.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    detail TEXT
);
"""


def now_iso():
    """Current time as an ISO string in UTC, e.g. 2026-10-16T22:10:05Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db():
    """
    One connection per web request (Flask's `g` object lives for one request).
    row_factory=sqlite3.Row lets us read columns by name: row["id"].
    """
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the tables if they don't exist yet. Safe to call every startup."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def query(sql, args=(), one=False):
    """Run a SELECT and return a list of dicts (or one dict if one=True)."""
    cur = get_db().execute(sql, args)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    """Run an INSERT/UPDATE and commit. Returns the new row id for inserts."""
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def log_action(user_id, action, target_type=None, target_id=None, detail=None):
    """Write one row into the audit trail."""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail)
    execute(
        "INSERT INTO audit_log (at, user_id, action, target_type, target_id, detail) VALUES (?,?,?,?,?,?)",
        (now_iso(), user_id, action, target_type, target_id, detail),
    )


def get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    if row is None:
        return default
    return json.loads(row["value"])


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
