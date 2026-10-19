"""Contrato `/api/v1` de entrega de eventos Agent → Hub (architecture §5). Sin secretos."""

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

AGENT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
_AGENT_ID = re.compile(AGENT_ID_PATTERN)
_INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def is_valid_agent_id(value: str) -> bool:
    return _AGENT_ID.match(value) is not None


def _check_uuid(value: str) -> str:
    # Forma canónica (minúsculas, con guiones): es la clave de deduplicación del Hub.
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        raise ValueError("no es un UUID") from None
    if canonical != value:
        raise ValueError("el UUID no está en su forma canónica")
    return value


def _check_instant(value: str) -> str:
    try:
        datetime.strptime(value, _INSTANT_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("el instante debe ser UTC con la forma YYYY-MM-DDTHH:MM:SSZ") from None
    return value


EventId = Annotated[str, AfterValidator(_check_uuid)]
Instant = Annotated[str, AfterValidator(_check_instant)]
AgentId = Annotated[str, Field(pattern=AGENT_ID_PATTERN)]


class AgentEvent(BaseModel):
    """Un evento del Agent. Campos desconocidos se ignoran (un Agent más nuevo no rompe al Hub).

    `record_name` es nulo para eventos sin registro (`agent_started`, `ip_*`).
    """

    event_id: EventId
    agent_id: AgentId
    sequence: Annotated[int, Field(ge=1)]
    type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    occurred_at: Instant
    record_name: Annotated[str, Field(max_length=253)] | None = None
    payload: dict[str, Any]


class EventBatch(BaseModel):
    """Lote de eventos. Cada elemento se valida por separado en el Hub (un evento inválido no
    rechaza el lote), por eso aquí viajan sin tipar y se validan con `AgentEvent`."""

    model_config = ConfigDict(extra="forbid")

    events: list[Any]


class RejectedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    code: str


class EventBatchResult(BaseModel):
    """`acked` incluye los duplicados (el reenvío es idempotente); `rejected` no se reenvía."""

    model_config = ConfigDict(extra="forbid")

    acked: list[str]
    rejected: list[RejectedEvent]


class ErrorResponse(BaseModel):
    """Forma de error común (architecture §5). `message_safe` nunca lleva detalles internos."""

    model_config = ConfigDict(extra="forbid")

    type: str
    code: str
    message_safe: str
    timestamp: Instant
    request_id: str
