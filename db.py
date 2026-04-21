import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional

from config import DB_PATH


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_user_id        INTEGER PRIMARY KEY,
                wallet_idx        INTEGER UNIQUE NOT NULL,
                address           TEXT    NOT NULL,
                paid              INTEGER NOT NULL DEFAULT 0,
                invite_sent       INTEGER NOT NULL DEFAULT 0,
                expired_reminded  INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Migrations: add new columns if absent (safe to run repeatedly)
        for ddl in [
            "ALTER TABLE users ADD COLUMN expired_reminded INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN payment_id TEXT",
        ]:
            try:
                con.execute(ddl)
            except Exception:
                pass


def next_wallet_index() -> int:
    with _conn() as con:
        row = con.execute("SELECT COALESCE(MAX(wallet_idx) + 1, 0) FROM users").fetchone()
        return int(row[0])


def get_user(tg_user_id: int) -> Optional[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,)
        ).fetchone()


def create_user(tg_user_id: int, wallet_idx: int, address: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (tg_user_id, wallet_idx, address) VALUES (?, ?, ?)",
            (tg_user_id, wallet_idx, address),
        )


def upsert_nowpayment(tg_user_id: int, pay_address: str, payment_id: str) -> None:
    """Create or refresh a NOWPayments order for an unpaid user."""
    with _conn() as con:
        # Try to insert; if user already exists and is unpaid, update payment details
        con.execute("""
            INSERT INTO users (tg_user_id, wallet_idx, address, payment_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                address    = excluded.address,
                payment_id = excluded.payment_id
            WHERE paid = 0
        """, (tg_user_id, tg_user_id, pay_address, payment_id))


def get_all_addresses() -> set:
    """Return a set of all addresses currently in the DB."""
    with _conn() as con:
        rows = con.execute("SELECT address FROM users").fetchall()
        return {r["address"] for r in rows}


def get_uninvited_users() -> list:
    """Return unpaid pending orders: paid=0 AND invite_sent=0."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM users WHERE paid = 0 AND invite_sent = 0"
        ).fetchall()
        return [dict(r) for r in rows]


def get_paid_uninvited() -> list:
    """Return paid orders that haven't received an invite yet: paid=1 AND invite_sent=0."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM users WHERE paid = 1 AND invite_sent = 0"
        ).fetchall()
        return [dict(r) for r in rows]


def reset_all_users() -> int:
    """Delete all rows from users table. Returns number of deleted rows."""
    with _conn() as con:
        cur = con.execute("DELETE FROM users")
        return cur.rowcount


def mark_paid(tg_user_id: int) -> None:
    with _conn() as con:
        con.execute("UPDATE users SET paid = 1 WHERE tg_user_id = ?", (tg_user_id,))


def mark_invite_sent(tg_user_id: int) -> None:
    with _conn() as con:
        con.execute("UPDATE users SET invite_sent = 1 WHERE tg_user_id = ?", (tg_user_id,))


def count_paid() -> int:
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) FROM users WHERE paid = 1").fetchone()
        return int(row[0])


def get_expiry_reminder_users(timeout_hours: int) -> list:
    """Return unpaid users older than timeout_hours who haven't been reminded yet."""
    with _conn() as con:
        rows = con.execute("""
            SELECT tg_user_id FROM users
            WHERE paid = 0
              AND expired_reminded = 0
              AND datetime(created_at) < datetime('now', ? || ' hours')
        """, (f"-{timeout_hours}",)).fetchall()
        return [dict(r) for r in rows]


def mark_expired_reminded(tg_user_id: int) -> None:
    with _conn() as con:
        con.execute("UPDATE users SET expired_reminded = 1 WHERE tg_user_id = ?", (tg_user_id,))
