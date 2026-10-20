"""Mutaciones administrativas: validar, persistir identidad/auditoría y después hacer red."""

import ssl
import uuid
from datetime import timedelta

from dyndnsarr_hub.clock import iso, parse_instant, utc_now
from dyndnsarr_hub.db import admin_repo, agents_repo, auth_repo, control_repo, private_repo
from dyndnsarr_hub.services import control, status_refresher
from dyndnsarr_hub.services.agent_client import AgentClient, build_http_client, safe_code
from dyndnsarr_shared.control import CommandRequest, ControlledConfig, EffectiveConfig
from dyndnsarr_shared.db import transaction


class ActionError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def target(conn, agent_id):
    try:
        url, token = agents_repo.get_api_target(conn, agent_id)
    except agents_repo.UnknownAgentError:
        raise ActionError("Agent desconocido", 404) from None
    enabled = {a.agent_id for a in agents_repo.list_agents(conn) if not a.disabled}
    if agent_id not in enabled or not url or not token:
        raise ActionError("El Agent no está habilitado o falta configurar su API", 409)
    return url, token


def prepare(conn, session, action, data, settings, now):
    """Toda la preparación corre en una sola transacción, sin await."""
    agent_id = data.get("agent_id") if action != "policy" else None
    key = data.get("operation_id", "")
    if not isinstance(key, str) or len(key) > 128:
        raise ActionError("Identificador de operación no válido")
    if action != "policy":
        target(conn, agent_id)
    fresh, reference = admin_repo.claim_operation(
        conn, key, session.digest, agent_id, action, data, now
    )
    if not fresh:
        return False, reference
    value = private_repo.snapshot(conn, agent_id) if agent_id else {}
    if action == "command":
        kind = data.get("type")
        if kind not in ("check_dns", "sync_ddns"):
            raise ActionError("Comando no autorizado")
        status = private_repo.status_model(value)
        if kind == "sync_ddns" and (
            status is None or not any(r.mode == "managed" for r in status.records)
        ):
            raise ActionError("No hay registros managed habilitados", 409)
        reference = str(uuid.uuid4())
        control_repo.create_command(
            conn,
            reference,
            agent_id,
            kind,
            f"admin:{session.user_id}",
            now,
            iso(now + timedelta(minutes=5)),
        )
    elif action == "config":
        if not value.get("config") or value.get("config_error"):
            raise ActionError(
                "Configuración no disponible; actualiza el Agent y el diagnóstico", 409
            )
        if now - parse_instant(value["config_at"]) > timedelta(
            seconds=3 * settings.status_refresh_seconds
        ):
            raise ActionError("Configuración obsoleta; actualiza el diagnóstico", 409)
        content = ControlledConfig.model_validate(data["content"]).model_dump(exclude_none=True)
        effective = EffectiveConfig.model_validate_json(value["config"])
        desired = control_repo.latest_desired(conn, agent_id)
        observed = control_repo.get_observed(conn, agent_id)
        if observed and observed.config_version > effective.version:
            raise admin_repo.ConflictError(
                "La configuración aplicada cambió; actualiza el diagnóstico"
            )
        if (
            int(data.get("desired_version", -1)) != (desired.version if desired else 0)
            or int(data.get("active_version", -1)) != effective.version
            or data.get("active_hash", "") != (effective.hash or "")
        ):
            raise admin_repo.ConflictError("La configuración cambió; recarga el formulario")
        desired = control_repo.add_desired_version(
            conn, agent_id, content, now, minimum_version=effective.version + 1
        )
        reference = str(desired.version)
    elif action == "resend":
        desired = control_repo.latest_desired(conn, agent_id)
        if desired is None or str(desired.version) != str(data.get("desired_version")):
            raise admin_repo.ConflictError("La versión deseada cambió; recarga el formulario")
        reference = str(desired.version)
    elif action == "command-refresh":
        command = control_repo.get_command(conn, data.get("command_id", ""))
        if not command or command.agent_id != agent_id:
            raise ActionError("Comando desconocido", 404)
        reference = command.command_id
    elif action == "policy":
        observation, critical, factor = [
            int(data[name])
            for name in (
                "observation_max_seconds",
                "critical_mismatch_seconds",
                "stale_refresh_factor",
            )
        ]
        maximum, _missing = private_repo.max_check_interval(conn)
        if not (
            max(1, maximum) <= observation <= 86400
            and 1 <= critical <= 2592000
            and 1 <= factor <= 100
        ):
            raise ActionError("Umbrales fuera de rango o menores que el intervalo del Agent")
        private_repo.update_policy(conn, observation, critical, factor)
        reference = "policy"
    elif action == "refresh":
        reference = "refresh"
    else:
        raise ActionError("Acción desconocida", 404)
    admin_repo.operation_reference(conn, key, reference)
    auth_repo.audit(conn, now, f"admin:{session.user_id}", action, agent_id, key)
    return True, reference


async def execute(conn, session, action, data, settings, transport=None):
    allowed = {
        "command": {"type"},
        "config": {"content", "desired_version", "active_version", "active_hash"},
        "resend": {"desired_version"},
        "command-refresh": {"command_id"},
        "refresh": set(),
        "policy": {"observation_max_seconds", "critical_mismatch_seconds", "stale_refresh_factor"},
    }
    if action not in allowed:
        raise ActionError("Acción desconocida", 404)
    if set(data) - (allowed[action] | {"operation_id", "agent_id"}):
        raise ActionError("Campos no permitidos")
    if any(not isinstance(value, (str, int)) for key, value in data.items() if key != "content"):
        raise ActionError("Campos no válidos")
    now = utc_now()
    try:
        with transaction(conn):
            fresh, reference = prepare(conn, session, action, data, settings, now)
    except admin_repo.ConflictError as exc:
        raise ActionError(str(exc), 409) from None
    if not fresh:
        return {"reference": reference, "duplicate": True}
    agent_id = data.get("agent_id")
    code, result = None, "succeeded"
    if action != "policy":
        try:
            url, token = target(conn, agent_id)
            async with build_http_client(settings.agent_ca_file, transport) as http:
                client = AgentClient(http, url, token)
                if action == "refresh":
                    outcome = await status_refresher.refresh_once(
                        conn, http, settings.status_refresh_seconds, agent_ids={agent_id}
                    )
                    code = outcome.get(agent_id)
                elif action in ("config", "resend"):
                    desired = control_repo.desired_version(conn, agent_id, int(reference))
                    reply = await control.send_config(conn, client, desired)
                    code = reply.error_code or reply.api_error
                    if reply.value is not None and reply.value.result == "rejected":
                        result, code = "rejected", reply.value.code
                    elif reply.value is not None:
                        await status_refresher.refresh_once(
                            conn, http, settings.status_refresh_seconds, agent_ids={agent_id}
                        )
                else:
                    command = control_repo.get_command(conn, reference)
                    if action == "command-refresh":
                        reply = await control.refresh_command(conn, client, command)
                    else:
                        request = CommandRequest(
                            command_id=command.command_id,
                            type=command.type,
                            requested_by=command.requested_by,
                            expires_at=command.expires_at,
                        )
                        reply = await control.send_command(conn, client, request, agent_id)
                    code = reply.error_code or reply.api_error
                    result = reply.value.status if reply.value else "uncertain"
            if code and result == "succeeded":
                result = "uncertain"
        except (OSError, ssl.SSLError):
            code, result = "tls_config", "uncertain"
    code = safe_code(code)
    with transaction(conn):
        auth_repo.audit(
            conn,
            utc_now(),
            f"admin:{session.user_id}",
            action,
            agent_id,
            data["operation_id"],
            result,
            code,
        )
    return {"reference": reference, "duplicate": False, "result": result, "code": code}
