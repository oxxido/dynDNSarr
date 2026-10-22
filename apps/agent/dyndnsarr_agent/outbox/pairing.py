"""Emparejamiento con el Hub por código compartido (ADR-0013, D2).

1. Desafío, por TLS aún sin verificar y sin secretos: el Hub devuelve su CA con un MAC. Si el MAC
   no cuadra (código distinto o un equipo en medio) el Agent no envía nada más.
2. Canje, por TLS verificado con esa CA: agent_id, URL y CA de la API y los dos tokens.

La tarea reintenta con espera creciente y no toca el DDNS. Errores como códigos seguros: nunca el
texto de una excepción, la URL, el código ni los tokens.
"""

import asyncio
import logging
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from dyndnsarr_agent.db import pairing_repo
from dyndnsarr_agent.outbox.loop import backoff_seconds, random_jitter
from dyndnsarr_agent.scheduler import utc_now
from dyndnsarr_shared.db import transaction
from dyndnsarr_shared.events import ErrorResponse
from dyndnsarr_shared.pairing import (
    CHALLENGE_LABEL,
    CLAIM_LABEL,
    CLAIMED_LABEL,
    ChallengeRequest,
    ChallengeResponse,
    ClaimRequest,
    ClaimResponse,
    check_ca_pem,
    mac,
    verify_mac,
)
from dyndnsarr_shared.tls import build_ssl_context, has_tls_error, unverified_bootstrap_context

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0
# Rechazos del Hub que se muestran tal cual; cualquier otro queda como `http_error`.
_HUB_CODES = frozenset(
    {"bad_mac", "code_consumed", "agent_exists", "rate_limited", "invalid_request"}
)
# Fallos que no se arreglan solos: van al log como error.
_NEEDS_ACTION = frozenset({"hub_mac_mismatch", "bad_mac", "code_consumed", "agent_exists"})

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ClaimContent:
    agent_id: str
    api_url: str
    api_ca_pem: str
    hub_token: SecretStr
    api_token: SecretStr


@dataclass(frozen=True)
class PairingOutcome:
    hub_ca_pem: str | None = None
    error_code: str | None = None


class Pairer:
    def __init__(
        self,
        base_url: str,
        code: SecretStr,
        content: ClaimContent,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url
        self._code = code
        self._content = content
        self._transport = transport

    async def pair_once(self) -> PairingOutcome:
        code = self._code.get_secret_value()
        nonce = secrets.token_hex(32)
        async with httpx.AsyncClient(
            verify=unverified_bootstrap_context(), transport=self._transport
        ) as http:
            challenge = await self._post(
                http, "/pair/v1/challenge", ChallengeRequest(nonce=nonce), ChallengeResponse
            )
        if isinstance(challenge, str):
            return PairingOutcome(error_code=challenge)
        if not verify_mac(code, challenge.mac, CHALLENGE_LABEL, *challenge.mac_parts(nonce)):
            return PairingOutcome(error_code="hub_mac_mismatch")
        try:
            check_ca_pem(challenge.ca_pem)
        except ValueError:
            return PairingOutcome(error_code="bad_response")

        c = self._content
        parts = (
            c.agent_id,
            c.api_url,
            c.api_ca_pem,
            c.hub_token.get_secret_value(),
            c.api_token.get_secret_value(),
        )
        request = ClaimRequest(
            agent_id=c.agent_id,
            api_url=c.api_url,
            api_ca_pem=c.api_ca_pem,
            hub_token=parts[3],
            api_token=parts[4],
            mac=mac(code, CLAIM_LABEL, *parts),
        )
        async with httpx.AsyncClient(
            verify=build_ssl_context(None, challenge.ca_pem), transport=self._transport
        ) as http:
            claimed = await self._post(http, "/pair/v1/claim", request, ClaimResponse)
        if isinstance(claimed, str):
            return PairingOutcome(error_code=claimed)
        if claimed.agent_id != c.agent_id or not verify_mac(
            code, claimed.mac, CLAIMED_LABEL, c.agent_id, request.mac
        ):
            return PairingOutcome(error_code="hub_mac_mismatch")
        return PairingOutcome(hub_ca_pem=challenge.ca_pem)

    async def _post(
        self, http: httpx.AsyncClient, path: str, body: BaseModel, model: type[T]
    ) -> T | str:
        try:
            response = await http.post(
                self._base + path,
                content=body.model_dump_json(),
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            return "timeout"
        except httpx.TransportError as exc:
            return "tls_error" if has_tls_error(exc) else "connect_error"
        status = response.status_code
        if status == 200:
            try:
                return model.model_validate_json(response.content)
            except ValidationError:
                return "bad_response"
        if status == 404:
            # Hub sin código, o el puerto no es el de enlace.
            return "pairing_unavailable"
        if status >= 500:
            return "server_error"
        try:
            code = ErrorResponse.model_validate_json(response.content).code
        except ValidationError:
            return "http_error"
        return code if code in _HUB_CODES else "http_error"


async def pair_until_done(
    conn: sqlite3.Connection,
    pairer: Pairer,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random_jitter,
) -> str:
    """Intenta emparejar hasta lograrlo (30 s ×2 hasta 15 min) y devuelve la CA del Hub, ya
    guardada. Cada intento queda en `app_state` para `pair-status`; el log solo registra los
    cambios de código."""
    failures = 0
    last_code = None
    while True:
        try:
            outcome = await pairer.pair_once()
        except Exception as exc:
            logger.error("pairing_error", extra={"error_type": type(exc).__name__})
            outcome = PairingOutcome(error_code="internal_error")
        now = clock()
        if outcome.hub_ca_pem is not None:
            with transaction(conn):
                pairing_repo.save_paired(conn, outcome.hub_ca_pem, now)
            logger.info("hub_paired", extra={"attempts": failures + 1})
            return outcome.hub_ca_pem
        code = outcome.error_code or "internal_error"
        with transaction(conn):
            pairing_repo.save_failure(conn, code, now)
        failures += 1
        if code != last_code:
            level = logging.ERROR if code in _NEEDS_ACTION else logging.WARNING
            logger.log(level, "pairing_failed", extra={"code": code})
            last_code = code
        await sleep(backoff_seconds(failures) * jitter())
