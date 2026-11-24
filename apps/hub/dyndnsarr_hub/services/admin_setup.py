"""Alta web del primer administrador con un token de un solo uso (ADR-0013, D5).

Con `ADMIN_ORIGIN` y sin administrador, cada arranque emite un token nuevo (solo su hash en
SQLite, caduca a las 24 h) y lo escribe en el log: es la excepción documentada a «sin secretos en
logs», válida solo hasta que exista el administrador.
"""

import hashlib
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta

from dyndnsarr_hub.auth import sessions
from dyndnsarr_hub.clock import iso, parse_instant
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import auth_repo
from dyndnsarr_shared.db import transaction

logger = logging.getLogger(__name__)

TTL = timedelta(hours=24)


class SetupError(ValueError):
    def __init__(self, message: str, status: int = 400, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _digest(raw: str) -> str:
    return hashlib.sha256(("admin-setup:" + raw).encode()).hexdigest()


def available(conn: sqlite3.Connection) -> bool:
    return auth_repo.user(conn) is None


def announce(conn: sqlite3.Connection, settings: Settings, now: datetime) -> None:
    """Emite el token de este arranque, o borra el que quedara si ya hay administrador."""
    if not available(conn):
        with transaction(conn):
            auth_repo.delete_admin_setup(conn)
        return
    if not settings.admin_origin:
        return
    raw = secrets.token_urlsafe(32)
    expires = now + TTL
    with transaction(conn):
        auth_repo.save_admin_setup(conn, _digest(raw), iso(expires))
    # La clave no contiene «token» a propósito: el filtro de redacción lo ocultaría (D5).
    logger.warning(
        "admin_setup_available",
        extra={
            "setup_url": f"{settings.admin_origin}/admin/setup",
            "setup_code": raw,
            "expires_at": iso(expires),
        },
    )


def create_admin(
    conn: sqlite3.Connection, raw: str, username: str, password: str, now: datetime
) -> None:
    """Crea el único administrador si el token vale; lo consume. Los fallos de token cuentan
    para el bloqueo global de credenciales (ADR-0009)."""
    wait = sessions.blocked_for(conn, now)
    if wait:
        auth_repo.audit(conn, now, "anonymous", "admin_setup", result="blocked")
        raise SetupError("Demasiados intentos; espera 15 min.", 429, wait)
    row = auth_repo.admin_setup(conn)
    valid = (
        row is not None
        and now < parse_instant(row[1])
        and secrets.compare_digest(_digest(raw).encode(), row[0].encode())
    )
    if not valid:
        sessions.record_failure(
            conn, now, auth_repo.throttle(conn), "admin_setup", "invalid_setup_token"
        )
        raise SetupError("Código de configuración no válido o caducado", 401)
    sessions.set_password(conn, username, password, now, create=True, actor="admin:setup")
    with transaction(conn):
        auth_repo.delete_admin_setup(conn)
