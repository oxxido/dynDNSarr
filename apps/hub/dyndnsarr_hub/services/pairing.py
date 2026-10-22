"""Emparejamiento Agent ↔ Hub por código compartido (ADR-0013). Reglas, sin HTTP."""

import logging
import sqlite3
from datetime import datetime, timedelta

from dyndnsarr_hub.auth.tokens import hash_token
from dyndnsarr_hub.clock import iso, parse_instant
from dyndnsarr_hub.db import agents_repo, pairing_repo
from dyndnsarr_hub.services.agent_client import check_api_url
from dyndnsarr_shared.control import canonical_json
from dyndnsarr_shared.db import transaction
from dyndnsarr_shared.pairing import (
    CHALLENGE_LABEL,
    CLAIM_LABEL,
    CLAIMED_LABEL,
    ChallengeRequest,
    ChallengeResponse,
    ClaimRequest,
    ClaimResponse,
    code_hash,
    mac,
    sha256_hex,
    verify_mac,
)

logger = logging.getLogger(__name__)

MAX_FAILURES = 10
WINDOW = timedelta(minutes=15)
BLOCK = timedelta(minutes=15)


class PairingError(Exception):
    def __init__(self, status: int, code: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.retry_after = retry_after


def _check_blocked(conn: sqlite3.Connection, now: datetime) -> None:
    row = pairing_repo.throttle(conn)
    if row and row[2] and now < parse_instant(row[2]):
        wait = max(1, int((parse_instant(row[2]) - now).total_seconds()))
        raise PairingError(429, "rate_limited", wait)


def _record_failure(conn: sqlite3.Connection, now: datetime) -> None:
    row = pairing_repo.throttle(conn)
    window, failures = iso(now), 0
    if row and now - parse_instant(row[0]) < WINDOW:
        window, failures = row[0], row[1]
    failures += 1
    blocked = iso(now + BLOCK) if failures >= MAX_FAILURES else None
    with transaction(conn):
        pairing_repo.save_throttle(conn, window, failures, blocked)


def challenge(
    conn: sqlite3.Connection, code: str, ca_pem: str, request: ChallengeRequest, now: datetime
) -> ChallengeResponse:
    """La CA del Hub con un MAC que demuestra conocer el código. No guarda nada."""
    _check_blocked(conn, now)
    return ChallengeResponse(
        ca_pem=ca_pem, mac=mac(code, CHALLENGE_LABEL, request.nonce, sha256_hex(ca_pem))
    )


def is_consumed(conn: sqlite3.Connection, code: str) -> bool:
    return pairing_repo.consumed(conn, code_hash(code)) is not None


def claim(
    conn: sqlite3.Connection, code: str, request: ClaimRequest, now: datetime
) -> ClaimResponse:
    """Registra al agente con sus tokens, su URL y su CA, y consume el código, todo en una
    transacción. El mismo canje repetido (respuesta perdida) devuelve lo mismo."""
    _check_blocked(conn, now)
    if not verify_mac(code, request.mac, CLAIM_LABEL, *request.mac_parts()):
        _record_failure(conn, now)
        raise PairingError(401, "bad_mac")
    try:
        api_url = check_api_url(request.api_url)
    except ValueError:
        raise PairingError(400, "invalid_request") from None
    content = sha256_hex(
        canonical_json(
            {
                "agent_id": request.agent_id,
                "api_url": api_url,
                "api_ca_sha256": sha256_hex(request.api_ca_pem),
                "hub_token_sha256": hash_token(request.hub_token),
                "api_token_sha256": hash_token(request.api_token),
            }
        )
    )
    key = code_hash(code)
    with transaction(conn):
        done = pairing_repo.consumed(conn, key)
        if done is not None and done != (request.agent_id, content):
            raise PairingError(409, "code_consumed")
        if done is None:
            try:
                agents_repo.create_agent(conn, request.agent_id, hash_token(request.hub_token), now)
            except agents_repo.AgentExistsError:
                raise PairingError(409, "agent_exists") from None
            agents_repo.set_api_url(conn, request.agent_id, api_url)
            agents_repo.set_api_token(conn, request.agent_id, request.api_token)
            agents_repo.set_api_ca_pem(conn, request.agent_id, request.api_ca_pem)
            pairing_repo.consume(conn, key, request.agent_id, content, now)
    if done is None:
        logger.info("agent_paired", extra={"agent_id": request.agent_id})
    return ClaimResponse(
        agent_id=request.agent_id, mac=mac(code, CLAIMED_LABEL, request.agent_id, request.mac)
    )
