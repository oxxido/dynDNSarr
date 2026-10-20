"""Vista pública: `GET /` (página) y `GET /public/v1/status` (el mismo DTO en JSON). Solo lectura.

La petición nunca llama al Agent ni lee `events`: solo la proyección pública (ADR-0007).
"""

import secrets
import sqlite3

import jinja2
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.public.builder import build_dto
from dyndnsarr_hub.public.view import build_view

router = APIRouter()

# Solo las respuestas públicas llevan estas cabeceras; una ruta inexistente no (ver `create_app`).
PUBLIC_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_templates = jinja2.Environment(
    loader=jinja2.PackageLoader("dyndnsarr_hub", "templates"), autoescape=True
)


def csp(nonce: str) -> str:
    """CSP por petición: el CSS y el JS inline de la página solo valen con el nonce de esa
    respuesta (sin `'unsafe-inline'`)."""
    return (
        "default-src 'none'; img-src 'self'; "
        f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


@router.get("/", response_model=None)
async def landing(request: Request) -> HTMLResponse:
    db: sqlite3.Connection = request.app.state.db
    dto = build_dto(db, utc_now(), request.app.state.settings)
    nonce = secrets.token_urlsafe(16)
    html = _templates.get_template("public/index.html.j2").render(v=build_view(dto), nonce=nonce)
    return HTMLResponse(html, headers={**PUBLIC_HEADERS, "Content-Security-Policy": csp(nonce)})


@router.get("/public/v1/status", response_model=None)
async def public_status(request: Request) -> JSONResponse:
    db: sqlite3.Connection = request.app.state.db
    dto = build_dto(db, utc_now(), request.app.state.settings)
    return JSONResponse(dto.model_dump(mode="json", by_alias=True), headers=PUBLIC_HEADERS)


@router.get("/public/v1/homarr", response_model=None)
async def homarr_status(request: Request):
    from dyndnsarr_hub.public.homarr import build

    return JSONResponse(
        build(request.app.state.db, utc_now(), request.app.state.settings).model_dump(mode="json"),
        headers=PUBLIC_HEADERS,
    )


@router.get("/public/homarr", response_model=None)
async def homarr_widget(request: Request):
    from dyndnsarr_hub.public.homarr import build

    settings = request.app.state.settings
    nonce = secrets.token_urlsafe(16)
    policy = csp(nonce).replace(
        "frame-ancestors 'none'", "frame-ancestors " + (settings.homarr_origin or "'none'")
    )
    html = _templates.get_template("public/homarr.j2").render(
        v=build(request.app.state.db, utc_now(), settings), nonce=nonce
    )
    return HTMLResponse(html, headers={**PUBLIC_HEADERS, "Content-Security-Policy": policy})
