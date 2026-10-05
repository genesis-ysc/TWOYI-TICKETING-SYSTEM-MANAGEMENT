"""
db.py
-----
Local database layer for the Twoyi Ticketing System, using SQLite.
No external account, API, or credentials needed — just a file on disk
(tickets.db, created automatically the first time the app runs).

Exposes the same shape as a Google-Sheets-backed version would, so
app.py doesn't need to know which storage it's talking to:

  BRANCHES, PRIORITIES, STATUSES, HEADERS
  get_all_tickets(), add_ticket(), mark_ticket_status()
"""

import os
import sqlite3
import datetime
from typing import Optional
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash

DB_FILE = os.environ.get("DATABASE_FILE", "tickets.db")

ROLES = ["admin", "client"]

# Seeded on first run only, if the users table is empty. Change these
# passwords immediately after first login (see README).
DEFAULT_USERS = [
    {"username": "admin", "password": "admin123", "role": "admin", "display_name": "Admin"},
    {"username": "client", "password": "client123", "role": "client", "display_name": "Branch Staff"},
]

TICKET_PREFIX = "Twoyi"
TICKET_NUMBER_DIGITS = 5

HEADERS = [
    "ID",
    "Ticket Number",
    "Branch",
    "Concern/Request",
    "Priority",
    "Date Created",
    "Date Finished",
    "Status",
    "Created By",
    "Closed By",
]

BRANCHES = [
    "Legazpi Village",
    "MOA Square",
    "Podium",
    "Rockwell",
    "Salcedo Village",
    "SM Aura",
    "Otaku",
    "BGC",
    "Opus Mall",
    "SM North Edsa",
    "The Corner House",
    "Aguirre",
]

PRIORITIES = ["High", "Medium", "Low"]
STATUSES = ["Open", "In Progress", "Closed"]


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

@contextmanager
def _connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_number TEXT NOT NULL,
                branch TEXT NOT NULL,
                concern TEXT NOT NULL,
                priority TEXT NOT NULL,
                date_created TEXT NOT NULL,
                date_finished TEXT,
                status TEXT NOT NULL DEFAULT 'Open',
                created_by TEXT NOT NULL DEFAULT '',
                closed_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                display_name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                verified INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        # Migrate older users tables created before email login existed.
        ucols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        if "email" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "verified" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 1")

        # Migrate older databases created before "created_by" existed.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tickets)")]
        if "created_by" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN created_by TEXT NOT NULL DEFAULT ''")
        if "closed_by" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN closed_by TEXT NOT NULL DEFAULT ''")

        # "Resolved" was renamed to "Closed".
        conn.execute("UPDATE tickets SET status = 'Closed' WHERE status = 'Resolved'")

        # Seed default accounts only if no users exist yet.
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if count == 0:
            for u in DEFAULT_USERS:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
                    (u["username"], generate_password_hash(u["password"]), u["role"], u["display_name"]),
                )


init_db()


# ---------------------------------------------------------------------------
# Users / auth
# ---------------------------------------------------------------------------

def _user_row(row) -> Optional[dict]:
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "role": row["role"],
        "display_name": row["display_name"],
        "email": row["email"] or "",
        "verified": bool(row["verified"]),
    }


def get_user_by_username(username: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        ).fetchone()
    return _user_row(row)


def get_user_by_email(email: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email != '' AND LOWER(email) = LOWER(?)", (email,)
        ).fetchone()
    return _user_row(row)


def get_user_by_login(identifier: str) -> Optional[dict]:
    """Find a user by email address or username (case-insensitive)."""
    return get_user_by_email(identifier) or get_user_by_username(identifier)


def verify_login(identifier: str, password: str) -> Optional[dict]:
    user = get_user_by_login(identifier)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def create_user(username: str, password: str, role: str, display_name: str,
                email: str = "", verified: bool = True) -> bool:
    if role not in ROLES:
        raise ValueError("role must be 'admin' or 'client'")
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, display_name, email, verified) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, generate_password_hash(password), role, display_name,
                 email.lower(), 1 if verified else 0),
            )
        return True
    except sqlite3.IntegrityError:
        return False  # username already taken


def mark_verified(email: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET verified = 1 WHERE LOWER(email) = LOWER(?)", (email,)
        )
        return cur.rowcount > 0


def change_password(username: str, new_password: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (generate_password_hash(new_password), username),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "ID": row["id"],
        "Ticket Number": row["ticket_number"],
        "Branch": row["branch"],
        "Concern/Request": row["concern"],
        "Priority": row["priority"],
        "Date Created": row["date_created"],
        "Date Finished": row["date_finished"] or "",
        "Status": row["status"],
        "Created By": row["created_by"] or "",
        "Closed By": row["closed_by"] or "",
    }


def get_all_tickets() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY id DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_tickets_by_creator(username: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE created_by = ? ORDER BY id DESC", (username,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_ticket_by_id(ticket_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def add_ticket(branch: str, concern: str, priority: str, created_by: str = "") -> dict:
    date_created = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO tickets (ticket_number, branch, concern, priority, date_created, status, created_by)
            VALUES ('', ?, ?, ?, ?, 'Open', ?)
            """,
            (branch, concern, priority, date_created, created_by),
        )
        new_id = cur.lastrowid
        ticket_number = f"{TICKET_PREFIX}{new_id:0{TICKET_NUMBER_DIGITS}d}"
        conn.execute(
            "UPDATE tickets SET ticket_number = ? WHERE id = ?",
            (ticket_number, new_id),
        )

    return {
        "ID": new_id,
        "Ticket Number": ticket_number,
        "Branch": branch,
        "Concern/Request": concern,
        "Priority": priority,
        "Date Created": date_created,
        "Date Finished": "",
        "Status": "Open",
        "Created By": created_by,
        "Closed By": "",
    }


def mark_ticket_status(ticket_id: int, status: str, changed_by: str = "") -> bool:
    """Update Status. Moving to Closed stamps Date Finished and records who
    closed it; moving back out of Closed (a reopen) clears both."""
    closing = status == "Closed"
    date_finished = datetime.datetime.now().strftime("%Y-%m-%d %H:%M") if closing else None
    closed_by = changed_by if closing else ""

    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tickets SET status = ?, date_finished = ?, closed_by = ? WHERE id = ?",
            (status, date_finished, closed_by, ticket_id),
        )
        return cur.rowcount > 0


def get_admin_emails() -> list[str]:
    """Email addresses of confirmed admin accounts (for notifications)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT email FROM users WHERE role = 'admin' AND email != '' AND verified = 1"
        ).fetchall()
    return [r["email"] for r in rows]
