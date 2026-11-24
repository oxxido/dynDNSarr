import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from dyndnsarr_hub import __version__, link_tls
from dyndnsarr_hub.api.events import router as events_router
from dyndnsarr_hub.api.health import router as health_router
from dyndnsarr_hub.api.pairing import router as pairing_router
from dyndnsarr_hub.api.public import router as public_router
from dyndnsarr_hub.auth.web import protect
from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, private_repo
from dyndnsarr_hub.db.state import record_boot
from dyndnsarr_hub.services import (
    admin_setup,
    alert_config,
    gateway,
    maintenance,
    pairing,
    status_refresher,
)
from dyndnsarr_hub.services.service_models import load_gateway
from dyndnsarr_shared.db import connect, migrate

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    conn = connect(settings.db_path)
    try:
        schema_version = migrate(conn, MIGRATIONS)
        instance_id, boot_count = record_boot(conn)
        app.state.db = conn
        app.state.gateway_policy = load_gateway(settings.gateway_file)
        app.state.service_lock = asyncio.Lock()
        try:
            app.state.alert_config = alert_config.load(settings.alerts_file)
        except Exception as exc:
            logger.error("alerts_config_failed", extra={"error_type": type(exc).__name__})
            app.state.alert_config = alert_config.AlertConfig()
            app.state.alert_config_error = True
        for agent_id, _snapshot in private_repo.enabled_snapshots(conn):
            private_repo.mark_gap(conn, agent_id, utc_now())
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
                "link_port": settings.link_port if settings.link_enabled else None,
            },
        )
        if settings.pairing_code is not None and pairing.is_consumed(
            conn, settings.pairing_code_value
        ):
            # Ya no sirve para emparejar: se puede borrar del `.env`.
            logger.info("pairing_code_consumed")
        admin_setup.announce(conn, settings, utc_now())
        refresher = asyncio.create_task(
            status_refresher.run_forever(
                conn,
                settings.agent_ca_file,
                settings.status_refresh_seconds,
                transport=app.state.agent_transport,
            )
        )
        maintenance_conn = connect(settings.db_path)
        maintenance_task = asyncio.create_task(
            maintenance.run(
                maintenance_conn,
                app.state.alert_config,
                settings,
                app.state.agent_transport,
            )
        )
        gateway_conn = connect(settings.db_path)
        gateway_task = asyncio.create_task(
            gateway.run(
                gateway_conn,
                app.state.gateway_policy,
                settings,
                app.state.service_lock,
                app.state.gateway_transport,
            )
        )
        try:
            yield
        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
            maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance_task
            maintenance_conn.close()
            gateway_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await gateway_task
            gateway_conn.close()
    finally:
        conn.close()


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="DynDNSarr Hub",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.alert_config_error = False
    # Puerto de enlace (ADR-0013): la CA y el certificado se crean o renuevan aquí, al arrancar.
    app.state.link_tls = None
    app.state.link_ca_pem = None
    if settings.link_enabled:
        app.state.link_tls = link_tls.ensure(settings.data_dir, settings.link_name_list, utc_now())
        app.state.link_ca_pem = app.state.link_tls.ca_pem
    # Transporte del cliente que consulta a los Agents: solo lo sustituyen las pruebas.
    app.state.agent_transport = None
    app.middleware("http")(protect)
    from dyndnsarr_hub.api.services import protect as service_protect
    from dyndnsarr_hub.api.services import router as service_router

    app.middleware("http")(service_protect)
    app.state.gateway_transport = None
    if settings.services_enabled:
        app.include_router(service_router)
    from dyndnsarr_hub.api.admin import router as admin_router

    app.include_router(admin_router)
    from dyndnsarr_hub.api.integrations import router as integrations_router

    app.include_router(integrations_router)
    app.mount(
        "/admin/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="admin-static"
    )
    app.include_router(health_router)
    app.include_router(events_router)
    if settings.pairing_code is not None:
        app.include_router(pairing_router)
    if settings.public_enabled:
        # Desactivada, las rutas públicas no existen: el 404 es el de cualquier ruta ausente.
        app.include_router(public_router)
        # `check_dir=False`: sin los PNG de la mascota (pendientes) el directorio no viaja en el
        # wheel; el Hub debe arrancar igualmente y dar 404 en `/static`.
        app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
    return app
