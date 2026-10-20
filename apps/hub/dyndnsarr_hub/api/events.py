import json
import logging
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from dyndnsarr_hub.auth.tokens import hash_token
from dyndnsarr_hub.clock import iso, utc_now
from dyndnsarr_hub.db import agents_repo, events_repo
from dyndnsarr_hub.services import events_handlers
from dyndnsarr_shared.db import transaction
from dyndnsarr_shared.events import (
    AgentEvent,
    ErrorResponse,
    EventBatch,
    EventBatchResult,
    RejectedEvent,
)

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_EVENTS_PER_BATCH = 100
MAX_BODY_BYTES = 256 * 1024
MAX_PAYLOAD_BYTES = 4 * 1024


class _BodyTooLargeError(Exception):
    pass


def _error(status: int, type_: str, code: str, message: str, headers: dict[str, str] | None = None):
    body = ErrorResponse(
        type=type_,
        code=code,
        message_safe=message,
        timestamp=iso(utc_now()),
        request_id=uuid.uuid4().hex,
    )
    return JSONResponse(body.model_dump(), status_code=status, headers=headers)


def _unauthorized() -> JSONResponse:
    # Mismo cuerpo para token ausente, malo, revocado, agente inexistente o deshabilitado.
    return _error(
        401,
        "auth_error",
        "unauthorized",
        "Credenciales no válidas",
        {"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def _read_body(request: Request) -> bytes:
    """Lee el cuerpo con tope también sin `Content-Length` (o con uno falso)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _BodyTooLargeError
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _BodyTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


def _validate(raw: Any, agent_id: str) -> AgentEvent | RejectedEvent:
    event_id = str(raw.get("event_id", ""))[:64] if isinstance(raw, dict) else ""
    try:
        event = AgentEvent.model_validate(raw)
    except ValidationError:
        return RejectedEvent(event_id=event_id, code="invalid_event")
    if event.agent_id != agent_id:
        return RejectedEvent(event_id=event_id, code="invalid_event")
    if len(json.dumps(event.payload).encode()) > MAX_PAYLOAD_BYTES:
        return RejectedEvent(event_id=event_id, code="payload_too_large")
    return event


@router.post("/api/v1/agents/{agent_id}/events", response_model=None)
async def post_events(agent_id: str, request: Request):
    db: sqlite3.Connection = request.app.state.db
    token = _bearer_token(request)
    token_agent = agents_repo.authenticate(db, hash_token(token)) if token else None
    if token_agent is None:
        logger.warning("events_auth_failed")
        return _unauthorized()
    if token_agent != agent_id:
        logger.warning("events_agent_mismatch")
        return _error(403, "auth_error", "forbidden", "El token no corresponde a este agente")
    try:
        body = await _read_body(request)
    except _BodyTooLargeError:
        return _error(413, "request_error", "body_too_large", "El cuerpo supera el límite")
    try:
        batch = EventBatch.model_validate_json(body)
    except ValidationError:
        return _error(400, "request_error", "invalid_batch", "El lote no tiene la forma esperada")
    if len(batch.events) > MAX_EVENTS_PER_BATCH:
        return _error(413, "request_error", "batch_too_large", "El lote supera el límite")

    checked = [_validate(raw, agent_id) for raw in batch.events]
    accepted = [c for c in checked if isinstance(c, AgentEvent)]
    rejected = [c for c in checked if isinstance(c, RejectedEvent)]
    now = utc_now()
    # El commit va antes de responder: el ACK solo se envía con los eventos ya guardados.
    with transaction(db):
        new = events_repo.insert_events(db, accepted, now)
        events_handlers.handle_new_events(db, new, now)
        from dyndnsarr_hub.db import alerts_repo

        for event in new:
            alerts_repo.observe(db, request.app.state.alert_config, event, now)
        agents_repo.touch_last_seen(db, agent_id, now)
    logger.info(
        "events_received",
        extra={"agent_id": agent_id, "accepted": len(accepted), "rejected": len(rejected)},
    )
    acked = list(dict.fromkeys(e.event_id for e in accepted))
    return EventBatchResult(acked=acked, rejected=rejected)
