"""Vista pública: `GET /public/v1/status` (el `PublicStatusDTO` en JSON). Solo lectura."""

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.public.builder import build_dto

router = APIRouter()

# Solo la respuesta pública lleva estas cabeceras; una ruta inexistente no (ver `create_app`).
PUBLIC_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@router.get("/public/v1/status", response_model=None)
async def public_status(request: Request) -> JSONResponse:
    db: sqlite3.Connection = request.app.state.db
    dto = build_dto(db, utc_now(), request.app.state.settings)
    return JSONResponse(dto.model_dump(mode="json", by_alias=True), headers=PUBLIC_HEADERS)
