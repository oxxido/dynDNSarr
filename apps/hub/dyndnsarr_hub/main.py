import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from dyndnsarr_hub import __version__
from dyndnsarr_hub.api.events import router as events_router
from dyndnsarr_hub.api.health import router as health_router
from dyndnsarr_hub.api.public import router as public_router
from dyndnsarr_hub.auth.web import protect
from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, private_repo
from dyndnsarr_hub.db.state import record_boot
from dyndnsarr_hub.services import alert_config, maintenance, status_refresher
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
            },
        )
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
    # Transporte del cliente que consulta a los Agents: solo lo sustituyen las pruebas.
    app.state.agent_transport = None
    app.middleware("http")(protect)
    from dyndnsarr_hub.api.admin import router as admin_router

    app.include_router(admin_router)
    from dyndnsarr_hub.api.integrations import router as integrations_router

    app.include_router(integrations_router)
    app.mount(
        "/admin/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="admin-static"
    )
    app.include_router(health_router)
    app.include_router(events_router)
    if settings.public_enabled:
        # Desactivada, las rutas públicas no existen: el 404 es el de cualquier ruta ausente.
        app.include_router(public_router)
        # `check_dir=False`: sin los PNG de la mascota (pendientes) el directorio no viaja en el
        # wheel; el Hub debe arrancar igualmente y dar 404 en `/static`.
        app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
    return app
