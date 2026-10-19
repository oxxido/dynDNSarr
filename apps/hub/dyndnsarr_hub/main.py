import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dyndnsarr_hub import __version__
from dyndnsarr_hub.api.events import router as events_router
from dyndnsarr_hub.api.health import router as health_router
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS
from dyndnsarr_hub.db.state import record_boot
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
        yield
    finally:
        conn.close()


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="DynDNSarr Hub", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health_router)
    app.include_router(events_router)
    return app
