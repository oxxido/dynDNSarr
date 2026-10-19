import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from dyndnsarr_agent import __version__
from dyndnsarr_agent.api.health import router as health_router
from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_agent.db.state import record_boot
from dyndnsarr_agent.ddns.config import (
    load_ddns_config,
    require_hub_token,
    require_provider_credentials,
)
from dyndnsarr_agent.dns.resolver import DnsObserver, DnspythonObserver
from dyndnsarr_agent.outbox.client import HttpHubClient, build_ssl_context
from dyndnsarr_agent.outbox.loop import run_forever as outbox_run_forever
from dyndnsarr_agent.providers.registry import build_providers
from dyndnsarr_agent.scheduler import run_forever, utc_now
from dyndnsarr_shared.db import connect, migrate

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    conn = connect(settings.db_path)
    try:
        schema_version = migrate(conn, MIGRATIONS)
        instance_id, boot_count = record_boot(conn)
        app.state.db = conn
        logger.info(
            "startup",
            extra={
                "version": __version__,
                "host": settings.host,
                "port": settings.port,
                "data_dir": str(settings.data_dir),
                "schema_version": schema_version,
                "instance_id": instance_id,
                "boot_count": boot_count,
            },
        )
        ddns_config = app.state.ddns_config
        if ddns_config is None:
            logger.info("ddns_engine_disabled")
            yield
            return
        ddns_repo.insert_event(
            conn,
            "agent_started",
            utc_now(),
            None,
            {"version": __version__, "boot_count": boot_count},
        )
        observer = app.state.ddns_observer or DnspythonObserver(ddns_config.dns.resolvers)
        async with contextlib.AsyncExitStack() as stack:
            # TLS verificado (valor por defecto de httpx); `transport` solo se inyecta en pruebas.
            client = await stack.enter_async_context(
                httpx.AsyncClient(transport=app.state.ddns_transport)
            )
            tasks = [
                asyncio.create_task(
                    run_forever(
                        conn,
                        ddns_config,
                        client,
                        observer,
                        providers=build_providers(ddns_config, settings, client),
                        lock_path=settings.lock_path,
                    )
                )
            ]
            app.state.ddns_task = tasks[0]
            hub = ddns_config.hub
            if hub is not None:
                # Cliente y conexión propios: la entrega no comparte nada con el ciclo DDNS.
                hub_http = await stack.enter_async_context(
                    httpx.AsyncClient(
                        verify=build_ssl_context(hub.ca_file), transport=app.state.hub_transport
                    )
                )
                outbox_conn = connect(settings.db_path)
                stack.callback(outbox_conn.close)
                hub_client = HttpHubClient(hub_http, hub.url, hub.agent_id, settings.hub_token)
                tasks.append(asyncio.create_task(outbox_run_forever(outbox_conn, hub_client)))
                app.state.outbox_task = tasks[-1]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
    finally:
        conn.close()


def create_app(
    settings: Settings,
    observer: DnsObserver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    hub_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Crea la app. Con `config_file` carga y valida el TOML y las credenciales que exija.

    Lanza `ConfigError` si algo no vale.
    """
    app = FastAPI(title="DynDNSarr Agent", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    ddns_config = load_ddns_config(settings.config_file) if settings.config_file else None
    if ddns_config is not None:
        require_provider_credentials(ddns_config, settings)
        require_hub_token(ddns_config, settings)
    app.state.ddns_config = ddns_config
    app.state.ddns_observer = observer
    app.state.ddns_transport = transport
    app.state.ddns_task = None
    app.state.hub_transport = hub_transport
    app.state.outbox_task = None
    app.include_router(health_router)
    return app
