"""Orquestación de control compartida por consola y administración web."""

from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.db import control_repo
from dyndnsarr_shared.control import ConfigPut
from dyndnsarr_shared.db import transaction


def record_command_reply(conn, command_id, agent_id, reply, *, submission=True):
    with transaction(conn):
        if reply.value is not None:
            control_repo.update_command_from_agent(
                conn, command_id, agent_id, reply.value.status, reply.value.result
            )
        elif submission and reply.api_error is not None:
            control_repo.update_command_from_agent(
                conn, command_id, agent_id, "failed", {"code": reply.api_error}
            )


async def send_config(conn, client, desired):
    put = ConfigPut(version=desired.version, hash=desired.content_hash, content=desired.content)
    reply = await client.put_config(put)
    if reply.value is not None:
        with transaction(conn):
            control_repo.record_config_result(
                conn,
                desired.agent_id,
                desired.version,
                desired.content_hash,
                reply.value.result,
                reply.value.code,
                utc_now(),
            )
    return reply


async def send_command(conn, client, request, agent_id):
    reply = await client.post_command(request)
    record_command_reply(conn, request.command_id, agent_id, reply)
    return reply


async def refresh_command(conn, client, command):
    reply = await client.command(command.command_id)
    record_command_reply(conn, command.command_id, command.agent_id, reply, submission=False)
    return reply


def command_details(command):
    """Solo veredictos conocidos por registro, sin volcar resultados libres del Agent."""
    from dyndnsarr_hub.services.agent_client import safe_code

    result = command.result or {}
    verdicts = {"verified", "mismatch", "unknown", "pending_verification", "no_reference"}
    records = result.get("records", {})
    details = (
        [
            {"name": name, "verdict": verdict}
            for name, verdict in records.items()
            if isinstance(name, str)
            and len(name) <= 253
            and isinstance(verdict, str)
            and verdict in verdicts
        ]
        if isinstance(records, dict)
        else []
    )
    return {"code": safe_code(result.get("code")), "records": details}
