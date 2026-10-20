"""API de servicios v1: Bearer propio, autenticación previa al cuerpo y errores seguros."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.db import auth_repo, services_repo
from dyndnsarr_hub.services.caddy import GatewayError
from dyndnsarr_hub.services.service_api import ServiceError, execute
from dyndnsarr_hub.services.service_models import TOOLS

router = APIRouter(prefix="/service/v1")


async def protect(request, call_next):
    if not request.url.path.startswith("/service/"):
        return await call_next(request)
    state = request.app.state
    if not state.settings.services_enabled:
        return JSONResponse({"code": "not_found"}, status_code=404)
    authorization = request.headers.get("authorization", "")
    identity = (
        services_repo.authenticate(state.db, authorization.removeprefix("Bearer "))
        if (authorization.startswith("Bearer ") and "origin" not in request.headers)
        else None
    )
    if identity is None:
        return JSONResponse(
            {"code": "unauthorized"},
            status_code=401,
            headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
        )
    request.state.service_identity = identity
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/tools")
async def list_tools(request: Request):
    _, grants = request.state.service_identity
    return {
        "tools": [
            {
                "name": name,
                "description": description,
                "inputSchema": model.model_json_schema(),
                "readOnly": kind == "read",
            }
            for name, (model, kind, description) in TOOLS.items()
            if name in grants.actions
        ]
    }


@router.post("/tools/{action}")
async def invoke(request: Request, action: str):
    state = request.app.state
    client_id, grants = request.state.service_identity
    if action not in TOOLS:
        return JSONResponse({"code": "not_found"}, status_code=404)
    if action not in grants.actions:
        auth_repo.audit(
            state.db, utc_now(), "client:" + client_id, action, result="denied", code="forbidden"
        )
        return JSONResponse({"code": "forbidden"}, status_code=403)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16384:
            return JSONResponse({"code": "body_too_large"}, status_code=413)
    try:
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ValueError
        data = TOOLS[action][0].model_validate_json(body)
    except (ValueError, ValidationError):
        return JSONResponse({"code": "invalid_request"}, status_code=400)
    try:
        # Un lock cubre preparación y red; nunca se mantiene una transacción SQLite abierta.
        async with state.service_lock:
            # Revalidar revocación después de esperar el lock.
            current = services_repo.client(state.db, client_id)
            if current is None:
                raise ServiceError("unauthorized", 401)
            result = await execute(
                state.db,
                client_id,
                current,
                action,
                data,
                state.settings,
                state.gateway_policy,
                agent_transport=state.agent_transport,
                gateway_transport=state.gateway_transport,
            )
        return JSONResponse(result)
    except (ServiceError, GatewayError) as exc:
        status = exc.status if isinstance(exc, ServiceError) else 409
        auth_repo.audit(
            state.db,
            utc_now(),
            "client:" + client_id,
            action,
            getattr(data, "domain", None) or getattr(data, "publication_id", None),
            str(data.operation_id) if hasattr(data, "operation_id") else None,
            "rejected",
            exc.code,
        )
        return JSONResponse({"code": exc.code}, status_code=status)
    except (OSError, ValueError):
        return JSONResponse({"code": "service_unavailable"}, status_code=503)
