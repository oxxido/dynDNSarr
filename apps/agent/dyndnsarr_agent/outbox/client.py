import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from pydantic import SecretStr, ValidationError

from dyndnsarr_agent.db.ddns_repo import Event
from dyndnsarr_shared.events import AgentEvent, EventBatch, EventBatchResult
from dyndnsarr_shared.tls import has_tls_error

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class DeliveryResult:
    """Resultado de enviar un lote. Con `error_code` no se sabe nada de los eventos enviados.

    Los códigos son seguros (D7): nunca el texto de una excepción ni la URL.
    """

    error_code: str | None = None
    acked: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


class HubClient(Protocol):
    async def send(self, events: list[Event]) -> DeliveryResult: ...


def _status_code(status: int) -> str:
    if status in (401, 403):
        return "auth_failed"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "http_error"


class HttpHubClient:
    """Entrega lotes al Hub por HTTPS. El `AsyncClient` trae la verificación TLS (ver `main`)."""

    def __init__(
        self, client: httpx.AsyncClient, base_url: str, agent_id: str, token: SecretStr
    ) -> None:
        self._client = client
        self._url = f"{base_url}/api/v1/agents/{agent_id}/events"
        self._agent_id = agent_id
        self._token = token

    async def send(self, events: list[Event]) -> DeliveryResult:
        batch = EventBatch(
            events=[
                AgentEvent(
                    event_id=e.event_id,
                    agent_id=self._agent_id,
                    sequence=e.sequence,
                    type=e.type,
                    occurred_at=e.occurred_at,
                    record_name=e.record_name,
                    payload=e.payload,
                ).model_dump()
                for e in events
            ]
        )
        try:
            response = await self._client.post(
                self._url,
                content=batch.model_dump_json(),
                headers={
                    "Authorization": f"Bearer {self._token.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            return DeliveryResult(error_code="timeout")
        except httpx.TransportError as exc:
            return DeliveryResult(error_code="tls_error" if has_tls_error(exc) else "connect_error")
        if response.status_code != 200:
            return DeliveryResult(error_code=_status_code(response.status_code))
        try:
            result = EventBatchResult.model_validate_json(response.content)
        except ValidationError:
            return DeliveryResult(error_code="bad_response")
        sent = {e.event_id for e in events}
        return DeliveryResult(
            acked=[i for i in result.acked if i in sent],
            rejected=[(r.event_id, r.code) for r in result.rejected if r.event_id in sent],
        )
