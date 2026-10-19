import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources.abc import Traversable
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    """Los archivos de migración no tienen el formato o la secuencia esperados."""


class SchemaVersionError(Exception):
    """La base de datos es de una versión posterior a la que conoce este código."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Abre la BD en modo WAL. Si no existe, la crea con permisos 0600.

    La conexión está en modo autocommit: usar `transaction()` para agrupar escrituras.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Solo al crearla: en POSIX, cerrar un descriptor de la BD libera los bloqueos que el
        # proceso tiene sobre ella, también los de otra conexión abierta (se pierden escrituras).
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _load_migrations(directory: Traversable) -> list[tuple[int, str]]:
    files = sorted(
        (f for f in directory.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name
    )
    scripts = []
    for expected, f in enumerate(files, start=1):
        match = _MIGRATION_NAME.match(f.name)
        if match is None:
            raise MigrationError(
                f"nombre de migración inválido: {f.name} (esperado NNNN_nombre.sql)"
            )
        if int(match.group(1)) != expected:
            raise MigrationError(f"falta la migración {expected:04d} antes de {f.name}")
        scripts.append((expected, f.read_text(encoding="utf-8")))
    return scripts


def migrate(conn: sqlite3.Connection, directory: Traversable) -> int:
    """Aplica en orden las migraciones pendientes, cada una en su transacción.

    La versión se guarda en `PRAGMA user_version`. Devuelve la versión resultante.
    """
    scripts = _load_migrations(directory)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(scripts):
        raise SchemaVersionError(
            f"la base de datos está en la versión {current}, "
            f"pero este código solo conoce hasta la {len(scripts)}"
        )
    for version, sql in scripts[current:]:
        try:
            conn.executescript(f"BEGIN;\n{sql}\n;\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return len(scripts)
