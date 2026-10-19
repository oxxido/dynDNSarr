import sqlite3
from dataclasses import dataclass
from datetime import datetime

from dyndnsarr_hub.clock import iso


class AgentExistsError(Exception):
    pass


class UnknownAgentError(Exception):
    pass


@dataclass(frozen=True)
class AgentRow:
    agent_id: str
    created_at: str
    last_seen_at: str | None
    disabled: bool
    active_tokens: int
    api_url: str | None
    has_api_token: bool


def create_agent(conn: sqlite3.Connection, agent_id: str, token_hash: str, now: datetime) -> None:
    """Crea el agente y su primer token en una transacción (la abre quien llama)."""
    try:
        conn.execute(
            "INSERT INTO agents (agent_id, created_at) VALUES (?, ?)", (agent_id, iso(now))
        )
    except sqlite3.IntegrityError:
        raise AgentExistsError(agent_id) from None
    _insert_credential(conn, agent_id, token_hash, now)


def add_token(conn: sqlite3.Connection, agent_id: str, token_hash: str, now: datetime) -> None:
    """Añade un token sin revocar los anteriores (rotación)."""
    if conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone() is None:
        raise UnknownAgentError(agent_id)
    # Un agente deshabilitado (`revoke --all`) vuelve a estar activo al emitirle un token.
    conn.execute("UPDATE agents SET disabled = 0 WHERE agent_id = ?", (agent_id,))
    _insert_credential(conn, agent_id, token_hash, now)


def _insert_credential(
    conn: sqlite3.Connection, agent_id: str, token_hash: str, now: datetime
) -> None:
    conn.execute(
        "INSERT INTO agent_credentials (agent_id, token_hash, created_at) VALUES (?, ?, ?)",
        (agent_id, token_hash, iso(now)),
    )


def revoke(conn: sqlite3.Connection, agent_id: str, now: datetime, everything: bool) -> int:
    """Revoca los tokens vigentes salvo el último emitido; con `everything`, todos y además
    deshabilita al agente. Devuelve cuántos tokens se revocaron."""
    if conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone() is None:
        raise UnknownAgentError(agent_id)
    if everything:
        conn.execute("UPDATE agents SET disabled = 1 WHERE agent_id = ?", (agent_id,))
        keep = None
    else:
        row = conn.execute(
            "SELECT max(id) FROM agent_credentials WHERE agent_id = ? AND revoked_at IS NULL",
            (agent_id,),
        ).fetchone()
        keep = row[0]
    cursor = conn.execute(
        "UPDATE agent_credentials SET revoked_at = ? "
        "WHERE agent_id = ? AND revoked_at IS NULL AND id IS NOT ?",
        (iso(now), agent_id, keep),
    )
    return cursor.rowcount


def authenticate(conn: sqlite3.Connection, token_hash: str) -> str | None:
    """`agent_id` dueño del token si está vigente y el agente habilitado; si no, `None`."""
    row = conn.execute(
        "SELECT c.agent_id FROM agent_credentials c JOIN agents a ON a.agent_id = c.agent_id "
        "WHERE c.token_hash = ? AND c.revoked_at IS NULL AND a.disabled = 0",
        (token_hash,),
    ).fetchone()
    return row[0] if row else None


def touch_last_seen(conn: sqlite3.Connection, agent_id: str, now: datetime) -> None:
    conn.execute("UPDATE agents SET last_seen_at = ? WHERE agent_id = ?", (iso(now), agent_id))


def list_agents(conn: sqlite3.Connection) -> list[AgentRow]:
    rows = conn.execute(
        "SELECT a.agent_id, a.created_at, a.last_seen_at, a.disabled, "
        "(SELECT count(*) FROM agent_credentials c "
        " WHERE c.agent_id = a.agent_id AND c.revoked_at IS NULL), "
        "a.api_url, a.api_token IS NOT NULL "
        "FROM agents a ORDER BY a.agent_id"
    ).fetchall()
    return [AgentRow(r[0], r[1], r[2], bool(r[3]), r[4], r[5], bool(r[6])) for r in rows]


def set_api_url(conn: sqlite3.Connection, agent_id: str, api_url: str) -> None:
    cursor = conn.execute("UPDATE agents SET api_url = ? WHERE agent_id = ?", (api_url, agent_id))
    if cursor.rowcount == 0:
        raise UnknownAgentError(agent_id)


def set_api_token(conn: sqlite3.Connection, agent_id: str, api_token: str) -> None:
    """Guarda el token con el que el Hub llama al Agent (en claro: ver ADR-0006)."""
    cursor = conn.execute(
        "UPDATE agents SET api_token = ? WHERE agent_id = ?", (api_token, agent_id)
    )
    if cursor.rowcount == 0:
        raise UnknownAgentError(agent_id)


def get_api_target(conn: sqlite3.Connection, agent_id: str) -> tuple[str | None, str | None]:
    """`(api_url, api_token)` del agente; `UnknownAgentError` si no existe."""
    row = conn.execute(
        "SELECT api_url, api_token FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise UnknownAgentError(agent_id)
    return row[0], row[1]


def api_targets(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """`(agent_id, api_url, api_token)` de los agentes habilitados con la API configurada."""
    rows = conn.execute(
        "SELECT agent_id, api_url, api_token FROM agents "
        "WHERE disabled = 0 AND api_url IS NOT NULL AND api_token IS NOT NULL ORDER BY agent_id"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]
