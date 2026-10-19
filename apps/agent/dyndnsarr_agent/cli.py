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
from dyndnsarr_agent.ddns.config import (
    ConfigError,
    DdnsConfig,
    load_ddns_config,
    require_provider_credentials,
)
from dyndnsarr_agent.ddns.cycle import classify, record_dns_samples, record_ip_samples, run_cycle
from dyndnsarr_agent.ddns.ip_sources import detect_public_ip
from dyndnsarr_agent.dns.resolver import DnsObserver, DnspythonObserver
from dyndnsarr_agent.logging_setup import setup_logging
from dyndnsarr_agent.main import create_app
from dyndnsarr_agent.providers.registry import build_providers
from dyndnsarr_agent.scheduler import try_lock, utc_now
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


def _hub_configured(settings: Settings) -> bool:
    """¿Hay sección `[hub]`? `status` no exige el TOML: sin él (o ilegible) no hay enlace."""
    if settings.config_file is None:
        return False
    try:
        return load_ddns_config(settings.config_file).hub is not None
    except ConfigError:
        return False


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
        records = ddns_repo.list_record_states(conn)
        events = ddns_repo.list_events(conn, limit=5)
        pending = ddns_repo.count_pending(conn)
        rejected = ddns_repo.count_rejected(conn)
        last_delivery = ddns_repo.get_state(conn, "outbox_last_success_at")
        last_error = ddns_repo.get_state(conn, "outbox_last_error_code")
        last_error_at = ddns_repo.get_state(conn, "outbox_last_error_at")
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
    for record in records:
        print(f"  {record.record_name}  {record.verification}  desde {record.since}")
        if record.accepted_ip:
            print(f"    aceptada por el proveedor: {record.accepted_ip} el {record.accepted_at}")
        if record.failures:
            failure = f"{record.failures} fallo(s), último error: {record.last_error_code}"
            if record.next_attempt_at:
                print(f"    reintento a partir de {record.next_attempt_at} ({failure})")
            else:
                print(f"    sin reintentos automáticos ({failure}): usa sync-ddns")
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
    if not _hub_configured(settings):
        print("Hub: sin configurar")
    else:
        # Nunca la URL ni el token: solo contadores y códigos seguros.
        print(f"Hub: {pending} evento(s) pendiente(s), {rejected} rechazado(s)")
        print(f"  último envío correcto: {last_delivery or 'nunca'}")
        if last_error:
            print(f"  último error: {last_error} (el {last_error_at})")
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


_DEDUPLICATED = {
    "verified": "el DNS ya resuelve la IP: sin llamada",
    "pending_verification": "IP ya aceptada, esperando propagación: sin llamada",
}


def _sync_ddns(
    settings: Settings,
    observer: DnsObserver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    **_: object,
) -> int:
    """Un pase manual de los registros `managed`: como el ciclo, pero sin esperar el backoff."""
    config = _load_ddns(settings)
    if config is None:
        return 1
    try:
        require_provider_credentials(config, settings)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    managed = [r for r in config.records if r.mode == "managed" and r.enabled]
    if not managed:
        print("No hay registros managed habilitados: nada que sincronizar.")
        return 0
    config = config.model_copy(update={"records": managed})
    observer = observer or DnspythonObserver(config.dns.resolvers)

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            providers = build_providers(config, settings, client)
            return await run_cycle(conn, config, client, observer, utc_now, providers, manual=True)

    conn = _open_db(settings)
    try:
        with try_lock(settings.lock_path) as taken:
            if not taken:
                print(
                    "Hay un ciclo del Agent en curso (ddns.lock): vuelve a intentarlo en unos "
                    "segundos.",
                    file=sys.stderr,
                )
                return 1
            result = asyncio.run(go())
    finally:
        conn.close()
    if result.ip_status != "confirmed":
        print("IP pública incierta: no se actualiza nada.", file=sys.stderr)
        return 1
    print(f"IP pública confirmada: {result.public_ip}")
    failed = False
    for name, verification in result.records.items():
        update = result.updates.get(name)
        if update is None:
            detail = _DEDUPLICATED.get(verification, "sin llamada")
        elif update.accepted:
            detail = "actualizado" if update.changed else "el proveedor ya la tenía"
        else:
            failed = True
            kind = "reintentable" if update.retryable else "definitivo"
            detail = f"fallo del proveedor: {update.error_code} ({kind})"
        print(f"  {name}  {verification}  {detail}")
    return 1 if failed else 0


_COMMANDS = {
    "status": _status,
    "check-ip": _check_ip,
    "check-dns": _check_dns,
    "sync-ddns": _sync_ddns,
}


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
    sub.add_parser(
        "sync-ddns",
        help="actualiza ahora los registros managed (ignora la espera de backoff)",
    )
    args = parser.parse_args(argv)
    if args.command in (None, "serve"):
        serve()
        return 0
    settings = _load_settings()
    return _COMMANDS[args.command](settings, observer=observer, transport=transport)


def cli() -> None:
    sys.exit(run(sys.argv[1:]))
