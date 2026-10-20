"""Persistencia de identidad web y auditoría, sin valores secretos en texto plano."""

import sqlite3
from datetime import datetime, timedelta

from dyndnsarr_hub.clock import iso


def audit(
    conn, now, actor, action, resource=None, operation_id=None, result="requested", code=None
):
    conn.execute(
        "INSERT INTO audit_log "
        "(occurred_at, actor, action, resource, operation_id, result, code) VALUES (?,?,?,?,?,?,?)",
        (iso(now), actor, action, resource, operation_id, result, code),
    )


def user(conn):
    return conn.execute("SELECT id, username, password_hash FROM users WHERE id = 1").fetchone()


def save_password(conn, username, encoded, create):
    if create:
        conn.execute("INSERT INTO users VALUES (1, ?, ?)", (username, encoded))
    else:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (encoded, username))
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM login_throttle")


def add_session(conn, digest, user_id, now, expires):
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?)",
        (digest, user_id, iso(now), iso(now), iso(expires)),
    )


def session(conn, digest):
    return conn.execute(
        "SELECT user_id, created_at, last_seen_at, expires_at FROM sessions WHERE token_hash = ?",
        (digest,),
    ).fetchone()


def touch_session(conn, digest, now):
    conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (iso(now), digest))


def revoke(conn, digest):
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))


def purge(conn: sqlite3.Connection, now: datetime):
    conn.execute(
        "DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions "
        "WHERE expires_at <= ? OR last_seen_at <= ? LIMIT 500)",
        (iso(now), iso(now - timedelta(minutes=30))),
    )


def throttle(conn):
    return conn.execute(
        "SELECT window_at, failures, blocked_until FROM login_throttle WHERE id = 1"
    ).fetchone()


def save_throttle(conn, window, failures, blocked_until):
    conn.execute(
        "INSERT OR REPLACE INTO login_throttle VALUES (1,?,?,?)", (window, failures, blocked_until)
    )


def clear_throttle(conn):
    conn.execute("DELETE FROM login_throttle")


def rehash(conn, encoded):
    conn.execute("UPDATE users SET password_hash = ? WHERE id = 1", (encoded,))
