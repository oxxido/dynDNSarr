import logging
import sqlite3

from fastapi import APIRouter, Request, Response

from dyndnsarr_hub import __version__
from dyndnsarr_shared.health import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request, response: Response) -> HealthResponse:
    status = "ok"
    try:
        request.app.state.db.execute("SELECT 1 FROM app_state LIMIT 1")
    except sqlite3.Error:
        logger.error("healthz: la base de datos no responde", exc_info=True)
        response.status_code = 503
        status = "error"
    return HealthResponse(status=status, component="hub", version=__version__)
