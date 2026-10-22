"""Autenticación del único administrador y sesiones opacas persistentes (slice5)."""

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from dyndnsarr_hub.clock import iso, parse_instant
from dyndnsarr_hub.db import auth_repo
from dyndnsarr_shared.db import transaction

COOKIE = "__Host-dyndnsarr_session"
HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
_DUMMY = HASHER.hash(secrets.token_urlsafe(32))


@dataclass(frozen=True)
class Session:
    digest: str
    user_id: int | None


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def csrf(raw: str) -> str:
    # Separación de propósito: no revela la cookie HttpOnly ni sirve como cookie.
    return hashlib.sha256(("csrf:" + raw).encode()).hexdigest()


def set_password(conn, username, password, now, *, create=False, actor="admin:cli"):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", username) or not 12 <= len(password) <= 128:
        raise ValueError("Usuario de 1–64 caracteres simples y contraseña de 12–128 caracteres")
    existing = auth_repo.user(conn)
    if (create and existing) or (not create and (not existing or existing[1] != username)):
        raise ValueError("Ya existe un administrador" if create else "Administrador desconocido")
    encoded = HASHER.hash(password)
    with transaction(conn):
        auth_repo.save_password(conn, username, encoded, create)
        auth_repo.audit(
            conn,
            now,
            actor,
            "admin_create" if create else "admin_password",
            result="succeeded",
        )


def create(conn, now, user_id=None):
    raw = secrets.token_urlsafe(32)
    ttl = timedelta(hours=12) if user_id else timedelta(minutes=10)
    with transaction(conn):
        auth_repo.purge(conn, now)
        auth_repo.add_session(conn, digest(raw), user_id, now, now + ttl)
    return raw, Session(digest(raw), user_id)


def get(conn, raw, now):
    if not raw or len(raw) > 128:
        return None
    key = digest(raw)
    row = auth_repo.session(conn, key)
    if row is None:
        return None
    user_id, _created, last_seen, expires = row
    if now >= parse_instant(expires) or now - parse_instant(last_seen) >= timedelta(minutes=30):
        auth_repo.revoke(conn, key)
        return None
    auth_repo.touch_session(conn, key, now)
    return Session(key, user_id)


def authenticate(conn: sqlite3.Connection, username: str, password: str, now: datetime):
    """Devuelve (user_id, segundos de bloqueo); todos los errores de credenciales son iguales."""
    throttle = auth_repo.throttle(conn)
    if throttle and throttle[2] and now < parse_instant(throttle[2]):
        wait = max(1, int((parse_instant(throttle[2]) - now).total_seconds()))
        auth_repo.audit(conn, now, "anonymous", "login", result="blocked")
        return None, wait
    user = auth_repo.user(conn)
    try:
        valid = HASHER.verify(user[2] if user else _DUMMY, password)
    except (VerificationError, InvalidHashError):
        valid = False
    if valid and user and secrets.compare_digest(username.encode(), user[1].encode()):
        with transaction(conn):
            auth_repo.clear_throttle(conn)
            if HASHER.check_needs_rehash(user[2]):
                auth_repo.rehash(conn, HASHER.hash(password))
            auth_repo.audit(conn, now, "admin:1", "login", result="succeeded")
        return 1, 0
    record_failure(conn, now, throttle, "login", "invalid_credentials")
    return None, 0


def blocked_for(conn, now):
    """Segundos que quedan de bloqueo global de credenciales (login y setup), o 0."""
    throttle = auth_repo.throttle(conn)
    if throttle and throttle[2] and now < parse_instant(throttle[2]):
        return max(1, int((parse_instant(throttle[2]) - now).total_seconds()))
    return 0


def record_failure(conn, now, throttle, action, code):
    window, failures = iso(now), 0
    if throttle and now - parse_instant(throttle[0]) < timedelta(minutes=15):
        window, failures = throttle[:2]
    failures += 1
    blocked = iso(now + timedelta(minutes=15)) if failures >= 5 else None
    with transaction(conn):
        auth_repo.save_throttle(conn, window, failures, blocked)
        auth_repo.audit(conn, now, "anonymous", action, result="failed", code=code)
