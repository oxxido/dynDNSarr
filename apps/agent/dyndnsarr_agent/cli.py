import argparse
import asyncio
import sqlite3
import sys
from collections.abc import Sequence

import httpx
import uvicorn
from pydantic import ValidationError

from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_agent.ddns.config import ConfigError, DdnsConfig, load_ddns_config
from dyndnsarr_agent.ddns.cycle import classify, record_dns_samples, record_ip_samples
from dyndnsarr_agent.ddns.ip_sources import detect_public_ip
from dyndnsarr_agent.dns.resolver import DnsObserver, DnspythonObserver
from dyndnsarr_agent.logging_setup import setup_logging
from dyndnsarr_agent.main import create_app
from dyndnsarr_agent.scheduler import utc_now
from dyndnsarr_shared.db import connect, migrate, transaction


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
    """Sirve el Agent (API + motor DDNS si hay configuración). Es el comando por defecto."""
    settings = _load_settings()
    setup_logging(settings.log_level)
    try:
        app = create_app(settings)
    except ConfigError as exc:
        sys.exit(str(exc))
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


def _status(settings: Settings, **_: object) -> int:
    if not settings.db_path.exists():
        print(
            f"No hay base de datos en {settings.db_path}: el Agent no ha arrancado.",
            file=sys.stderr,
        )
        return 1
    # Solo lectura: `status` nunca escribe.
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        ip = ddns_repo.get_state(conn, "public_ip")
        ip_status = ddns_repo.get_state(conn, "ip_status")
        confirmed_at = ddns_repo.get_state(conn, "public_ip_confirmed_at")
        last_check = conn.execute("SELECT max(observed_at) FROM public_ip_observations").fetchone()[
            0
        ]
        records = conn.execute(
            "SELECT record_name, verification, since FROM record_state ORDER BY record_name"
        ).fetchall()
        events = ddns_repo.list_events(conn, limit=5)
    except sqlite3.OperationalError:
        print(
            "La base de datos no tiene datos del motor DDNS: arranca el Agent una vez.",
            file=sys.stderr,
        )
        return 1
    finally:
        conn.close()
    print(f"Base de datos: {settings.db_path}")
    print(f"Última comprobación: {last_check or 'nunca'}")
    if ip_status is None:
        print("IP pública: desconocida (sin observaciones)")
    elif ip_status == "confirmed":
        print(f"IP pública: {ip} (confirmada el {confirmed_at})")
    else:
        print(f"IP pública: incierta (última confirmada: {ip or 'ninguna'}, el {confirmed_at})")
    print("Registros:")
    for name, verification, since in records:
        print(f"  {name}  {verification}  desde {since}")
    if not records:
        print("  (ninguno observado)")
    print("Últimos eventos:")
    for e in events:
        print(
            f"  #{e.sequence} {e.occurred_at} {e.type}"
            + (f" {e.record_name}" if e.record_name else "")
        )
    if not events:
        print("  (ninguno)")
    return 0


def _load_ddns(settings: Settings) -> DdnsConfig | None:
    if settings.config_file is None:
        print(
            "Falta DYNDNSARR_AGENT_CONFIG_FILE: no hay fuentes ni registros configurados.",
            file=sys.stderr,
        )
        return None
    try:
        return load_ddns_config(settings.config_file)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return None


def _open_db(settings: Settings) -> sqlite3.Connection:
    conn = connect(settings.db_path)
    migrate(conn, MIGRATIONS)
    return conn


def _check_ip(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None, **_: object
) -> int:
    config = _load_ddns(settings)
    if config is None:
        return 1

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await detect_public_ip(client, config.ip_sources.urls)

    results, outcome = asyncio.run(go())
    conn = _open_db(settings)
    try:
        with transaction(conn):
            record_ip_samples(conn, utc_now(), results)
    finally:
        conn.close()
    for r in results:
        print(f"  {r.source}  {r.ip or 'error: ' + str(r.error_code)}")
    if outcome.status == "confirmed":
        print(f"IP pública confirmada: {outcome.ip}")
        return 0
    print(f"IP pública incierta ({outcome.reason})", file=sys.stderr)
    return 1


def _check_dns(settings: Settings, observer: DnsObserver | None = None, **_: object) -> int:
    config = _load_ddns(settings)
    if config is None:
        return 1
    observer = observer or DnspythonObserver(config.dns.resolvers)
    names = [r.name for r in config.records if r.enabled]

    async def go():
        return await asyncio.gather(*(observer.lookup(n) for n in names))

    all_answers = asyncio.run(go())
    conn = _open_db(settings)
    try:
        known_ip = ddns_repo.get_state(conn, "public_ip")
        with transaction(conn):
            for name, answers in zip(names, all_answers, strict=True):
                record_dns_samples(conn, utc_now(), name, answers)
    finally:
        conn.close()
    print(f"IP pública de referencia: {known_ip or 'desconocida'} (última confirmada)")
    for name, answers in zip(names, all_answers, strict=True):
        verdict = classify(known_ip, answers) if known_ip else "sin referencia"
        print(f"{name}  {verdict}")
        for a in answers:
            print(f"  {a.resolver}  {','.join(a.ips) or 'error: ' + str(a.error_code)}")
    return 0


_COMMANDS = {"status": _status, "check-ip": _check_ip, "check-dns": _check_dns}


def run(
    argv: Sequence[str],
    observer: DnsObserver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="dyndnsarr-agent")
    sub = parser.add_subparsers(dest="command", metavar="comando")
    sub.add_parser("serve", help="sirve el Agent (por defecto, sin comando)")
    sub.add_parser("status", help="muestra el estado guardado (solo lectura)")
    sub.add_parser("check-ip", help="detecta la IP pública y guarda las muestras")
    sub.add_parser(
        "check-dns", help="consulta los resolvers por los registros y guarda las muestras"
    )
    args = parser.parse_args(argv)
    if args.command in (None, "serve"):
        serve()
        return 0
    settings = _load_settings()
    return _COMMANDS[args.command](settings, observer=observer, transport=transport)


def cli() -> None:
    sys.exit(run(sys.argv[1:]))
