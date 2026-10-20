import argparse
import asyncio
import json
import sqlite3
import ssl
import sys
import tomllib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path

import httpx
import uvicorn
from pydantic import ValidationError

from dyndnsarr_hub.auth.tokens import generate_api_token, generate_token, hash_token
from dyndnsarr_hub.clock import iso, utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, control_repo, events_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_hub.services.agent_client import (
    AgentClient,
    Reply,
    build_http_client,
    check_api_url,
)
from dyndnsarr_shared.control import (
    CommandRequest,
    CommandStatus,
    canonical_json,
)
from dyndnsarr_shared.db import connect, migrate, transaction
from dyndnsarr_shared.events import AGENT_ID_PATTERN, is_valid_agent_id
from dyndnsarr_shared.logging import configure_logging


def _load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Sin el valor recibido: a futuro la configuración puede incluir secretos.
        prefix = Settings.model_config["env_prefix"]
        problems = "\n".join(
            f"  {prefix}{'_'.join(map(str, e['loc'])).upper()}: {e['msg']}"
            for e in exc.errors(include_input=False)
        )
        sys.exit(f"Configuración inválida:\n{problems}")


def serve() -> None:
    """Sirve el Hub. Es el comando por defecto."""
    settings = _load_settings()
    configure_logging(settings.log_level)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)


def _open_db(settings: Settings) -> sqlite3.Connection:
    conn = connect(settings.db_path)
    migrate(conn, MIGRATIONS)
    return conn


def _print_token(agent_id: str, token: str) -> None:
    print(f"Agente: {agent_id}")
    print("Token (se muestra una sola vez; el Hub solo guarda su hash):")
    print(token)


def _agent_add(settings: Settings, args: argparse.Namespace) -> int:
    if not is_valid_agent_id(args.agent_id):
        print(f"agent_id no válido: debe cumplir {AGENT_ID_PATTERN}", file=sys.stderr)
        return 1
    token = generate_token()
    conn = _open_db(settings)
    try:
        with transaction(conn):
            agents_repo.create_agent(conn, args.agent_id, hash_token(token), utc_now())
    except agents_repo.AgentExistsError:
        print(f"El agente {args.agent_id} ya existe (usa agent-rotate).", file=sys.stderr)
        return 1
    finally:
        conn.close()
    _print_token(args.agent_id, token)
    return 0


def _agent_rotate(settings: Settings, args: argparse.Namespace) -> int:
    token = generate_token()
    conn = _open_db(settings)
    try:
        with transaction(conn):
            agents_repo.add_token(conn, args.agent_id, hash_token(token), utc_now())
    except agents_repo.UnknownAgentError:
        print(f"El agente {args.agent_id} no existe.", file=sys.stderr)
        return 1
    finally:
        conn.close()
    _print_token(args.agent_id, token)
    print("El token anterior sigue vigente: ejecuta agent-revoke cuando el Agent use el nuevo.")
    return 0


def _agent_revoke(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_db(settings)
    try:
        with transaction(conn):
            revoked = agents_repo.revoke(conn, args.agent_id, utc_now(), args.all)
    except agents_repo.UnknownAgentError:
        print(f"El agente {args.agent_id} no existe.", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if args.all:
        print(f"{revoked} token(s) revocado(s); {args.agent_id} queda deshabilitado.")
    else:
        print(f"{revoked} token(s) revocado(s); queda vigente solo el último emitido.")
    return 0


def _agent_list(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_db(settings)
    try:
        agents = agents_repo.list_agents(conn)
    finally:
        conn.close()
    if not agents:
        print("(sin agentes)")
    for a in agents:
        state = "deshabilitado" if a.disabled else "activo"
        api = (a.api_url or "sin configurar") + ("" if a.has_api_token else " (sin token)")
        print(
            f"{a.agent_id}  {state}  tokens vigentes: {a.active_tokens}  "
            f"creado: {a.created_at}  visto: {a.last_seen_at or 'nunca'}  api: {api}"
        )
    return 0


def _events(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_db(settings)
    try:
        events = events_repo.list_events(conn, args.agent, args.limit)
    finally:
        conn.close()
    if not events:
        print("(sin eventos)")
    for e in events:
        print(
            f"{e.agent_id} #{e.sequence} {e.occurred_at} {e.type}"
            + (f" {e.record_name}" if e.record_name else "")
            + f" recibido: {e.received_at} {json.dumps(e.payload, sort_keys=True)}"
        )
    return 0


class CliError(Exception):
    """Un error de uso o de entorno que se muestra tal cual y termina con código 1."""


# --- control del Agent (slice 3b) ---


def _api_target(conn: sqlite3.Connection, agent_id: str) -> tuple[str, str]:
    try:
        url, token = agents_repo.get_api_target(conn, agent_id)
    except agents_repo.UnknownAgentError:
        raise CliError(f"El agente {agent_id} no existe.") from None
    if not url:
        raise CliError(
            f"Falta la URL de la API del Agent: dyndnsarr-hub agent-set-api {agent_id} <url>"
        )
    if not token:
        raise CliError(f"Falta el token de la API: dyndnsarr-hub agent-api-token {agent_id}")
    return url, token


def _call_agent(
    settings: Settings,
    url: str,
    token: str,
    transport: httpx.AsyncBaseTransport | None,
    action: Callable[[AgentClient], Awaitable[Reply]],
) -> Reply:
    """Una llamada al Agent con TLS verificado (solo la CA configurada o el almacén del sistema)."""

    async def go() -> Reply:
        async with build_http_client(settings.agent_ca_file, transport) as http:
            return await action(AgentClient(http, url, token))

    try:
        return asyncio.run(go())
    except (OSError, ssl.SSLError):
        raise CliError(
            "No se puede leer la CA de los Agents (DYNDNSARR_HUB_AGENT_CA_FILE); "
            "ver docs/operations.md"
        ) from None


def _agent_set_api(settings: Settings, args: argparse.Namespace) -> int:
    try:
        url = check_api_url(args.url)
    except ValueError as exc:
        raise CliError(f"URL no válida: {exc}") from None
    conn = _open_db(settings)
    try:
        with transaction(conn):
            agents_repo.set_api_url(conn, args.agent_id, url)
    except agents_repo.UnknownAgentError:
        raise CliError(f"El agente {args.agent_id} no existe.") from None
    finally:
        conn.close()
    print(f"API del Agent {args.agent_id}: {url}")
    return 0


def _agent_api_token(settings: Settings, args: argparse.Namespace) -> int:
    token = generate_api_token()
    conn = _open_db(settings)
    try:
        with transaction(conn):
            agents_repo.set_api_token(conn, args.agent_id, token)
    except agents_repo.UnknownAgentError:
        raise CliError(f"El agente {args.agent_id} no existe.") from None
    finally:
        conn.close()
    print(f"Agente: {args.agent_id}")
    print("Token de la API del Agent (se muestra una sola vez):")
    print(token)
    print(
        "Ponlo en DYNDNSARR_AGENT_API_TOKEN (agent.env) y reinicia el Agent; hasta entonces "
        "el Agent responderá 401 al Hub."
    )
    return 0


def _read_content(path: Path) -> dict:
    try:
        content = tomllib.loads(path.read_text(encoding="utf-8"))
        canonical_json(content)
    except OSError as exc:
        raise CliError(f"No se puede leer {path}: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise CliError(f"{path}: TOML inválido: {exc}") from None
    except TypeError:
        raise CliError(
            f"{path}: contiene valores que no se pueden enviar como JSON (¿fechas?)"
        ) from None
    return content


def _send_config(
    conn: sqlite3.Connection,
    settings: Settings,
    desired: control_repo.DesiredVersion,
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    url, token = _api_target(conn, desired.agent_id)
    from dyndnsarr_hub.services.control import send_config

    reply = _call_agent(
        settings, url, token, transport, lambda client: send_config(conn, client, desired)
    )
    if reply.value is None:
        detail = reply.error_code or reply.api_error or "bad_response"
        print(
            f"No se pudo enviar la versión {desired.version} ({detail}): queda pendiente; "
            f"reenvíala con config-resend {desired.agent_id}.",
            file=sys.stderr,
        )
        return 1
    result = reply.value
    if result.result == "applied":
        print(f"Versión {desired.version}: aplicada por el Agent.")
        return 0
    print(f"Versión {desired.version}: rechazada por el Agent ({result.code}).", file=sys.stderr)
    if result.message_safe:
        print(result.message_safe, file=sys.stderr)
    return 1


def _config_push(
    settings: Settings, args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None
) -> int:
    content = _read_content(args.file)
    conn = _open_db(settings)
    try:
        _api_target(conn, args.agent_id)  # falla antes de crear nada si no se puede enviar
        with transaction(conn):
            desired = control_repo.add_desired_version(conn, args.agent_id, content, utc_now())
        print(f"Versión {desired.version} creada (hash {desired.content_hash[:12]}).")
        return _send_config(conn, settings, desired, transport)
    finally:
        conn.close()


def _config_resend(
    settings: Settings, args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None
) -> int:
    conn = _open_db(settings)
    try:
        desired = control_repo.latest_desired(conn, args.agent_id)
        if desired is None:
            raise CliError(f"No hay ninguna versión creada para {args.agent_id}: usa config-push.")
        return _send_config(conn, settings, desired, transport)
    finally:
        conn.close()


def _config_show(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open_db(settings)
    try:
        desired = control_repo.latest_desired(conn, args.agent_id)
        observed = control_repo.get_observed(conn, args.agent_id)
    finally:
        conn.close()
    if desired is None:
        print("Versión deseada: ninguna")
    else:
        print(
            f"Versión deseada: {desired.version} (hash {desired.content_hash[:12]}, "
            f"creada {desired.created_at})"
        )
    if observed is None or observed.config_version == 0:
        print("Versión aplicada en el Agent: ninguna (manda su TOML)")
    else:
        print(
            f"Versión aplicada en el Agent: {observed.config_version} "
            f"(hash {(observed.config_hash or '')[:12]})"
        )
    if observed is not None and observed.last_attempt_version is not None:
        code = f" ({observed.last_code})" if observed.last_code else ""
        print(
            f"Último intento: versión {observed.last_attempt_version} {observed.last_result}"
            f"{code} el {observed.last_attempt_at}"
        )
    if desired is not None:
        pending = observed is None or observed.config_version != desired.version
        print("Estado: " + ("pendiente de aplicar" if pending else "aplicada"))
        print("Contenido:")
        print(json.dumps(desired.content, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _agent_status(
    settings: Settings, args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None
) -> int:
    conn = _open_db(settings)
    try:
        url, token = _api_target(conn, args.agent_id)
    finally:
        conn.close()

    async def both(client: AgentClient):
        return await client.health(), await client.status()

    health, status = _call_agent(settings, url, token, transport, both)
    for reply in (health, status):
        if reply.value is None:
            detail = reply.error_code or reply.api_error or "bad_response"
            print(f"No se pudo consultar al Agent ({detail}).", file=sys.stderr)
            return 1
    h, s = health.value, status.value
    print(f"Agente {args.agent_id}: versión {h.version}, estado {h.status}, hora {h.time_utc}")
    print(f"Última comprobación DNS local: {h.last_local_dns_check_at or 'nunca'}")
    if s.config_version:
        print(
            f"Configuración activa: versión {s.config_version} (hash {(s.config_hash or '')[:12]})"
        )
    else:
        print("Configuración activa: TOML local")
    if s.ip_status is None:
        print("IP pública: desconocida (sin observaciones)")
    elif s.ip_status == "confirmed":
        print(f"IP pública: {s.public_ip} (confirmada el {s.public_ip_confirmed_at})")
    else:
        print(f"IP pública: incierta (última confirmada: {s.public_ip or 'ninguna'})")
    print("Registros:")
    for r in s.records:
        print(f"  {r.name}  {r.mode}  {r.verification}  desde {r.since or '-'}")
    if not s.records:
        print("  (ninguno)")
    if s.hub is not None:
        print(
            f"Enlace con el Hub: {s.hub.pending_events} evento(s) pendiente(s), "
            f"{s.hub.rejected_events} rechazado(s), último envío "
            f"{s.hub.last_success_at or 'nunca'}"
        )
    return 0


def _print_command(command: control_repo.HubCommand) -> None:
    print(f"Comando {command.command_id}: {command.type} -> {command.status}")
    print(
        f"  pedido por {command.requested_by} el {command.created_at}, caduca {command.expires_at}"
    )
    if command.result is not None:
        print(f"  resultado: {json.dumps(command.result, sort_keys=True)}")


def _apply_command_reply(
    conn: sqlite3.Connection, command_id: str, agent_id: str, reply: Reply[CommandStatus]
) -> None:
    """Refleja la respuesta del `POST`: el estado del Agent, o su rechazo explícito (el comando
    nunca se ejecutará, así que `failed` es definitivo)."""
    from dyndnsarr_hub.services.control import record_command_reply

    record_command_reply(conn, command_id, agent_id, reply)


def _command(
    settings: Settings, args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None
) -> int:
    now = utc_now()
    try:
        request = CommandRequest(
            command_id=str(uuid.uuid4()),
            type=args.type,
            requested_by="admin:cli",
            expires_at=iso(now + timedelta(seconds=args.ttl)),
            resource_id=args.resource_id,
        )
    except ValidationError:
        raise CliError("Tipo de comando no válido.") from None
    conn = _open_db(settings)
    try:
        url, token = _api_target(conn, args.agent_id)
        with transaction(conn):
            control_repo.create_command(
                conn,
                request.command_id,
                args.agent_id,
                request.type,
                request.requested_by,
                now,
                request.expires_at,
                resource_id=request.resource_id,
            )
        print(f"Comando {request.command_id} creado.")
        reply = _call_agent(
            settings, url, token, transport, lambda client: client.post_command(request)
        )
        _apply_command_reply(conn, request.command_id, args.agent_id, reply)
        if reply.value is None and reply.api_error is None:
            print(
                f"No se pudo enviar ({reply.error_code}): comprueba su estado con "
                f"command-show --refresh {request.command_id}; si no llegó, pide otro comando.",
                file=sys.stderr,
            )
            return 1
        _print_command(control_repo.get_command(conn, request.command_id))
        return 1 if reply.value is None else 0
    finally:
        conn.close()


def _command_show(
    settings: Settings, args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None
) -> int:
    conn = _open_db(settings)
    try:
        command = control_repo.get_command(conn, args.command_id)
        if command is None:
            raise CliError(f"No existe el comando {args.command_id}.")
        if args.refresh:
            url, token = _api_target(conn, command.agent_id)
            reply = _call_agent(
                settings, url, token, transport, lambda client: client.command(args.command_id)
            )
            if reply.value is not None:
                _apply_command_reply(conn, args.command_id, command.agent_id, reply)
            elif reply.api_error == "not_found":
                # No es definitivo: el POST pudo quedar en vuelo y el evento aún puede llegar.
                print("El Agent no conoce ese comando (no llegó, o ya se purgó).", file=sys.stderr)
            else:
                detail = reply.error_code or reply.api_error or "bad_response"
                print(f"No se pudo consultar al Agent ({detail}).", file=sys.stderr)
            command = control_repo.get_command(conn, args.command_id)
        _print_command(command)
    finally:
        conn.close()
    return 0


def _admin_password(settings, args):
    from getpass import getpass

    from dyndnsarr_hub.auth.sessions import set_password

    password = getpass("Contraseña (12–128 caracteres): ")
    if password != getpass("Repite la contraseña: "):
        raise CliError("Las contraseñas no coinciden")
    conn = _open_db(settings)
    try:
        set_password(
            conn, args.username, password, utc_now(), create=args.command == "admin-create"
        )
    except ValueError as exc:
        raise CliError(str(exc)) from None
    finally:
        conn.close()
    print("Administrador actualizado; sesiones y bloqueo revocados.")
    return 0


def _service_client(settings, args):
    from pydantic import TypeAdapter

    from dyndnsarr_hub.db import auth_repo, services_repo
    from dyndnsarr_hub.services.service_models import Grants, Id

    if args.command != "client-list":
        try:
            TypeAdapter(Id).validate_python(args.client_id)
        except ValidationError:
            raise CliError("ID de cliente no válido") from None
    grants = None
    if args.command == "client-add":
        try:
            grants = Grants.model_validate(tomllib.loads(args.file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            raise CliError("Archivo de permisos no válido; no se ha creado el cliente") from None
    conn = _open_db(settings)
    try:
        if args.command == "client-add":
            try:
                with transaction(conn):
                    token = services_repo.add_client(conn, args.client_id, grants, utc_now())
            except sqlite3.IntegrityError:
                raise CliError("El cliente ya existe; revócalo y utiliza otro ID") from None
            print("Token de servicio (se muestra una sola vez):")
            print(token)
        elif args.command == "client-revoke":
            with transaction(conn):
                changed = conn.execute(
                    "UPDATE service_clients SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (iso(utc_now()), args.client_id),
                )
                if changed.rowcount == 0:
                    raise CliError("Cliente no encontrado o ya revocado")
                auth_repo.audit(
                    conn, utc_now(), "cli", "client_revoke", args.client_id, result="succeeded"
                )
            print("Cliente revocado")
        else:
            for client_id, created, revoked in conn.execute(
                "SELECT id,created_at,revoked_at FROM service_clients ORDER BY id"
            ):
                print(
                    f"{client_id}  creado: {created}  estado: {'revocado' if revoked else 'activo'}"
                )
    finally:
        conn.close()
    return 0


_COMMANDS = {
    "client-add": _service_client,
    "client-revoke": _service_client,
    "client-list": _service_client,
    "agent-add": _agent_add,
    "agent-rotate": _agent_rotate,
    "agent-revoke": _agent_revoke,
    "agent-list": _agent_list,
    "events": _events,
    "agent-set-api": _agent_set_api,
    "agent-api-token": _agent_api_token,
    "config-show": _config_show,
}

# Los que llaman al Agent reciben además el transporte (solo pruebas).
_NETWORK_COMMANDS = {
    "config-push": _config_push,
    "config-resend": _config_resend,
    "agent-status": _agent_status,
    "command": _command,
    "command-show": _command_show,
}


def run(argv: Sequence[str], transport: httpx.AsyncBaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dyndnsarr-hub")
    sub = parser.add_subparsers(dest="command", metavar="comando")
    sub.add_parser("serve", help="sirve el Hub (por defecto, sin comando)")
    for name in ("admin-create", "admin-password"):
        admin = sub.add_parser(name, help="aprovisiona o recupera al administrador web")
        admin.add_argument("username")
    add = sub.add_parser("agent-add", help="registra un agente y emite su primer token")
    add.add_argument("agent_id")
    rotate = sub.add_parser("agent-rotate", help="emite un token nuevo sin revocar los anteriores")
    rotate.add_argument("agent_id")
    revoke = sub.add_parser(
        "agent-revoke", help="revoca los tokens salvo el último emitido (--all: todos)"
    )
    revoke.add_argument("agent_id")
    revoke.add_argument("--all", action="store_true", help="revoca todos y deshabilita al agente")
    sub.add_parser("agent-list", help="lista los agentes (sin tokens ni hashes)")
    events = sub.add_parser("events", help="muestra los últimos eventos recibidos")
    events.add_argument("--agent", help="solo los de este agent_id")
    events.add_argument("--limit", type=int, default=20)
    set_api = sub.add_parser("agent-set-api", help="guarda la URL de la API de control del Agent")
    set_api.add_argument("agent_id")
    set_api.add_argument("url")
    api_token = sub.add_parser(
        "agent-api-token", help="emite el token con el que el Hub llama a ese Agent"
    )
    api_token.add_argument("agent_id")
    push = sub.add_parser("config-push", help="crea la versión siguiente desde un TOML y la envía")
    push.add_argument("agent_id")
    push.add_argument("file", type=Path)
    resend = sub.add_parser("config-resend", help="reenvía la última versión creada")
    resend.add_argument("agent_id")
    show = sub.add_parser("config-show", help="muestra la versión deseada y la aplicada")
    show.add_argument("agent_id")
    status = sub.add_parser("agent-status", help="consulta health y status al Agent")
    status.add_argument("agent_id")
    command = sub.add_parser("command", help="pide check_dns, sync_ddns o wake_device al Agent")
    command.add_argument("agent_id")
    command.add_argument("type")
    command.add_argument("--resource-id", help="ID local del dispositivo para wake_device")
    command.add_argument("--ttl", type=int, default=300, help="segundos de validez (300)")
    shown = sub.add_parser("command-show", help="muestra el estado de un comando")
    shown.add_argument("command_id")
    shown.add_argument("--refresh", action="store_true", help="pregunta antes al Agent")
    client_add = sub.add_parser("client-add", help="emite token scoped desde permisos TOML")
    client_add.add_argument("client_id")
    client_add.add_argument("file", type=Path)
    client_revoke = sub.add_parser("client-revoke", help="revoca una identidad API/MCP")
    client_revoke.add_argument("client_id")
    sub.add_parser("client-list", help="lista identidades API/MCP sin tokens ni hashes")
    args = parser.parse_args(argv)
    if args.command in (None, "serve"):
        serve()
        return 0
    try:
        settings = _load_settings()
        if args.command in _NETWORK_COMMANDS:
            return _NETWORK_COMMANDS[args.command](settings, args, transport)
        if args.command in ("admin-create", "admin-password"):
            return _admin_password(settings, args)
        return _COMMANDS[args.command](settings, args)
    except CliError as exc:
        print(exc, file=sys.stderr)
        return 1


def cli() -> None:
    sys.exit(run(sys.argv[1:]))
