"""Consultas acotadas para administración e idempotencia de formularios."""

import json
import secrets
from datetime import timedelta

from dyndnsarr_hub.clock import iso, parse_instant
from dyndnsarr_shared.control import config_hash


class ConflictError(ValueError):
    pass


def issue_operation(conn, session_hash, agent_id, action, now):
    key = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO web_operations VALUES (?,?,?,?,?,NULL,?)",
        (key, session_hash, agent_id, action, "", iso(now)),
    )
    conn.execute(
        "DELETE FROM web_operations WHERE operation_id IN (SELECT operation_id "
        "FROM web_operations WHERE created_at < ? LIMIT 500)",
        (iso(now - timedelta(days=1)),),
    )
    return key


def claim_operation(conn, key, session_hash, agent_id, action, content, now):
    row = conn.execute(
        "SELECT session_hash, agent_id, action, content_hash, reference, created_at "
        "FROM web_operations WHERE operation_id = ?",
        (key,),
    ).fetchone()
    if (
        row is None
        or row[:3] != (session_hash, agent_id, action)
        or now - parse_instant(row[5]) > timedelta(hours=12)
    ):
        raise ConflictError("Operación no válida; recarga el formulario")
    fingerprint = config_hash(content)
    if row[3] and row[3] != fingerprint:
        raise ConflictError("El identificador ya se utilizó con otro contenido")
    if row[3]:
        return False, row[4]
    conn.execute(
        "UPDATE web_operations SET content_hash = ? WHERE operation_id = ?", (fingerprint, key)
    )
    return True, None


def operation_reference(conn, key, reference):
    conn.execute("UPDATE web_operations SET reference = ? WHERE operation_id = ?", (reference, key))


def boot_info(conn):
    return [
        dict(key=r[0], value=r[1], updated_at=r[2])
        for r in conn.execute(
            "SELECT key,value,updated_at FROM app_state WHERE key IN ('instance_id','boot_count')"
        )
    ]


def history(
    conn, *, agent=None, record=None, event_type=None, since=None, until=None, cursor=None, limit=50
):
    clauses, params = [], []
    for field, value in [("agent_id", agent), ("record_name", record), ("type", event_type)]:
        if value:
            clauses.append(field + " = ?")
            params.append(value)
    for op, value in [(">=", since), ("<=", until)]:
        if value:
            parse_instant(value)
            clauses.append("occurred_at " + op + " ?")
            params.append(value)
    if cursor:
        position = json.loads(cursor)
        if (
            not isinstance(position, list)
            or len(position) != 3
            or not all(isinstance(v, str) for v in position)
        ):
            raise ValueError("Cursor no válido")
        clauses.append("(received_at, agent_id, event_id) < (?,?,?)")
        params.extend(position)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        "SELECT agent_id,event_id,type,occurred_at,received_at,record_name,payload "
        "FROM events" + where + " ORDER BY received_at DESC,agent_id DESC,event_id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    entries = []
    # El payload entrante es libre. Solo textos/valores comprobados de estos campos salen a UI.
    safe_codes = {
        "timeout",
        "connect_error",
        "tls_error",
        "nxdomain",
        "servfail",
        "no_answer",
        "auth_failed",
        "busy",
        "interrupted",
        "no_managed_records",
        "invalid_config",
    }
    for a, event_id, kind, at, received, name, payload in rows[:limit]:
        value = json.loads(payload)
        command_id = value.get("command_id")
        command = (
            conn.execute(
                "SELECT requested_by FROM commands WHERE command_id = ? AND agent_id = ?",
                (command_id, a),
            ).fetchone()
            if isinstance(command_id, str)
            else None
        )
        code = value.get("code")
        entries.append(
            dict(
                agent_id=a,
                event_id=event_id,
                type=kind,
                occurred_at=at,
                received_at=received,
                record_name=name,
                origin=command[0] if command else "no determinado",
                code=code if isinstance(code, str) and code in safe_codes else None,
            )
        )
    next_cursor = (
        json.dumps([rows[limit - 1][4], rows[limit - 1][0], rows[limit - 1][1]])
        if len(rows) > limit
        else None
    )
    return {"entries": entries, "next_cursor": next_cursor}


def audit_entries(conn, limit=50):
    cursor = conn.execute(
        "SELECT occurred_at,actor,action,resource,operation_id,result,code "
        "FROM audit_log ORDER BY occurred_at DESC,id DESC LIMIT ?",
        (limit,),
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor]
