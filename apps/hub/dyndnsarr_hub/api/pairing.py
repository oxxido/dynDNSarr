"""Puerto de enlace de los Agents (ADR-0013): `/pair/v1` y la misma `/api/v1` del puerto HTTP.

El servidor de enlace (HTTPS propio) sirve la app envuelta en `link_app`, que solo deja pasar
esos prefijos y marca la petición; `/pair/v1` responde 404 sin esa marca (puerto HTTP).
"""

import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from dyndnsarr_hub.clock import iso, utc_now
from dyndnsarr_hub.services import pairing
from dyndnsarr_shared.events import ErrorResponse
from dyndnsarr_shared.pairing import ChallengeRequest, ClaimRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pair/v1")

LINK_SCOPE_KEY = "dyndnsarr.link"
LINK_PREFIXES = ("/api/v1/", "/pair/v1/")
MAX_BODY_BYTES = 32 * 1024


def link_app(app):
    """ASGI del puerto de enlace: fuera de `LINK_PREFIXES` todo es 404."""

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            if not scope["path"].startswith(LINK_PREFIXES):
                response = JSONResponse({"message_safe": "No encontrado"}, status_code=404)
                await response(scope, receive, send)
                return
            scope = {**scope, LINK_SCOPE_KEY: True}
        await app(scope, receive, send)

    return wrapped


def _error(status: int, code: str, message: str, retry_after: int | None = None) -> JSONResponse:
    body = ErrorResponse(
        type="pairing_error",
        code=code,
        message_safe=message,
        timestamp=iso(utc_now()),
        request_id=uuid.uuid4().hex,
    )
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse(body.model_dump(), status_code=status, headers=headers)


_MESSAGES = {
    "bad_mac": "El código no coincide",
    "code_consumed": "El código ya se usó: genera otro en los dos lados",
    "agent_exists": "Ya existe un agente con ese agent_id",
    "rate_limited": "Demasiados intentos; espera 15 min",
    "invalid_request": "La petición no tiene la forma esperada",
}


async def _parse(request: Request, model: type[BaseModel]) -> BaseModel | JSONResponse:
    if not request.scope.get(LINK_SCOPE_KEY):
        return _error(404, "not_found", "No encontrado")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return _error(413, "body_too_large", "El cuerpo supera el límite")
        chunks.append(chunk)
    try:
        return model.model_validate_json(b"".join(chunks))
    except ValidationError:
        return _error(400, "invalid_request", _MESSAGES["invalid_request"])


def _rejected(exc: pairing.PairingError) -> JSONResponse:
    # Solo el código: nunca el contenido de la petición (lleva tokens).
    logger.warning("pairing_rejected", extra={"code": exc.code})
    return _error(exc.status, exc.code, _MESSAGES[exc.code], exc.retry_after)


@router.post("/challenge", response_model=None)
async def post_challenge(request: Request):
    parsed = await _parse(request, ChallengeRequest)
    if isinstance(parsed, JSONResponse):
        return parsed
    settings = request.app.state.settings
    try:
        return pairing.challenge(
            request.app.state.db,
            settings.pairing_code_value,
            request.app.state.link_ca_pem,
            parsed,
            utc_now(),
        )
    except pairing.PairingError as exc:
        return _rejected(exc)


@router.post("/claim", response_model=None)
async def post_claim(request: Request):
    parsed = await _parse(request, ClaimRequest)
    if isinstance(parsed, JSONResponse):
        return parsed
    settings = request.app.state.settings
    try:
        return pairing.claim(request.app.state.db, settings.pairing_code_value, parsed, utc_now())
    except pairing.PairingError as exc:
        return _rejected(exc)
