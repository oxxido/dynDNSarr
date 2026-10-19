import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence

import uvicorn
from pydantic import ValidationError

from dyndnsarr_hub.auth.tokens import generate_token, hash_token
from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, events_repo
from dyndnsarr_hub.main import create_app
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
        print(
            f"{a.agent_id}  {state}  tokens vigentes: {a.active_tokens}  "
            f"creado: {a.created_at}  visto: {a.last_seen_at or 'nunca'}"
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


_COMMANDS = {
    "agent-add": _agent_add,
    "agent-rotate": _agent_rotate,
    "agent-revoke": _agent_revoke,
    "agent-list": _agent_list,
    "events": _events,
}


def run(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="dyndnsarr-hub")
    sub = parser.add_subparsers(dest="command", metavar="comando")
    sub.add_parser("serve", help="sirve el Hub (por defecto, sin comando)")
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
    args = parser.parse_args(argv)
    if args.command in (None, "serve"):
        serve()
        return 0
    return _COMMANDS[args.command](_load_settings(), args)


def cli() -> None:
    sys.exit(run(sys.argv[1:]))
