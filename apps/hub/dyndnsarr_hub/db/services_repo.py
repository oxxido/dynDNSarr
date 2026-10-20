"""Identidades revocables, operaciones y revisiones propias; nunca tokens recuperables."""

import json
import secrets
from hashlib import sha256

from dyndnsarr_hub.clock import iso
from dyndnsarr_hub.db import auth_repo
from dyndnsarr_hub.services.service_models import Grants
from dyndnsarr_shared.control import canonical_json


def add_client(conn, client_id, grants, now):
    token = "dds_" + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO service_clients VALUES (?,?,?,?,NULL)",
        (client_id, sha256(token.encode()).hexdigest(), grants.model_dump_json(), iso(now)),
    )
    auth_repo.audit(conn, now, "cli", "client_add", client_id, result="succeeded")
    return token


def authenticate(conn, token):
    if not token.startswith("dds_") or len(token) > 128:
        return None
    row = conn.execute(
        "SELECT id,grants FROM service_clients WHERE token_hash=? AND revoked_at IS NULL",
        (sha256(token.encode()).hexdigest(),),
    ).fetchone()
    return (row[0], Grants.model_validate_json(row[1])) if row else None


def client(conn, client_id):
    row = conn.execute(
        "SELECT grants FROM service_clients WHERE id=? AND revoked_at IS NULL", (client_id,)
    ).fetchone()
    return Grants.model_validate_json(row[0]) if row else None


def operation(conn, key):
    cursor = conn.execute("SELECT * FROM service_operations WHERE id=?", (str(key),))
    row = cursor.fetchone()
    if not row:
        return None
    value = dict(zip([c[0] for c in cursor.description], row, strict=True))
    for field in ("request", "response"):
        value[field] = json.loads(value[field]) if value[field] else None
    return value


def reserve(conn, key, client_id, action, resource, request, now):
    content = canonical_json(request)
    conn.execute(
        "INSERT INTO service_operations "
        "(id,client_id,action,resource,fingerprint,request,created_at) VALUES (?,?,?,?,?,?,?)",
        (
            str(key),
            client_id,
            action,
            resource,
            sha256(content.encode()).hexdigest(),
            content,
            iso(now),
        ),
    )
    auth_repo.audit(conn, now, "client:" + client_id, action, resource, str(key))


def finish(conn, key, state, response, now):
    op = operation(conn, key)
    if op["state"] == state and op["response"] == response:
        return
    conn.execute(
        "UPDATE service_operations SET state=?,response=? WHERE id=?",
        (state, canonical_json(response), str(key)),
    )
    auth_repo.audit(
        conn,
        now,
        "client:" + op["client_id"],
        op["action"],
        op["resource"],
        str(key),
        state,
        response.get("code"),
    )


def publications(conn):
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT content FROM publications WHERE content IS NOT NULL ORDER BY id"
        )
    ]


def publication(conn, key):
    row = conn.execute("SELECT revision,content FROM publications WHERE id=?", (key,)).fetchone()
    return (row[0], json.loads(row[1]) if row[1] else None) if row else (0, None)


def save_publication(conn, key, content, operation_id, now):
    revision, _ = publication(conn, key)
    conn.execute("DELETE FROM publication_checks WHERE publication_id=?", (key,))
    revision += 1
    if content is not None:
        content = {**content, "revision": revision}
    encoded = canonical_json(content) if content is not None else None
    conn.execute(
        "INSERT INTO publications VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "revision=excluded.revision,content=excluded.content",
        (key, revision, encoded),
    )
    conn.execute(
        "INSERT INTO publication_revisions VALUES (?,?,?,?,?)",
        (key, revision, encoded, operation_id, iso(now)),
    )
    return content


def record_command_result(conn, command_id, now):
    """HTTP y eventos comparten el resultado persistido; solo estados finales son definitivos."""
    from dyndnsarr_hub.db import control_repo
    from dyndnsarr_hub.services.agent_client import safe_code

    op = operation(conn, command_id)
    command = control_repo.get_command(conn, command_id)
    if not op or not command or command.status not in ("succeeded", "failed", "expired"):
        return
    finish(
        conn,
        command_id,
        "succeeded" if command.status == "succeeded" else "failed",
        {
            "command_id": command_id,
            "status": command.status,
            "code": safe_code((command.result or {}).get("code")),
        },
        now,
    )
