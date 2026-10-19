"""Aplicar una configuración remota (ADR-0006, D5): validar todo y, solo entonces, confirmar."""

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime

from pydantic import ValidationError

from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.db import control_repo, ddns_repo
from dyndnsarr_agent.ddns.active_config import CONTROLLED_SECTIONS, ConfigHolder, overlay
from dyndnsarr_agent.ddns.config import (
    ConfigError,
    DdnsConfig,
    describe_errors,
    require_provider_credentials,
)
from dyndnsarr_agent.scheduler import utc_now
from dyndnsarr_shared.control import ConfigPut, ConfigResult, canonical_json, config_hash
from dyndnsarr_shared.db import transaction

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1024


def _reject(
    conn: sqlite3.Connection, put: ConfigPut, at: datetime, code: str, message: str
) -> ConfigResult:
    # El evento lleva versión, hash y código; nunca el contenido.
    with transaction(conn):
        ddns_repo.insert_event(
            conn,
            "config_rejected",
            at,
            None,
            {"version": put.version, "hash": put.hash, "code": code},
        )
    logger.warning("config_rejected", extra={"version": put.version, "code": code})
    return ConfigResult(
        result="rejected",
        version=put.version,
        code=code,
        message_safe=message[:MAX_MESSAGE_CHARS],
    )


def apply_config(
    conn: sqlite3.Connection,
    holder: ConfigHolder,
    settings: Settings,
    put: ConfigPut,
    clock: Callable[[], datetime] = utc_now,
) -> ConfigResult:
    """Valida `put` entero y, si vale, guarda la versión y cambia la configuración en vigor.

    Es síncrona (sin `await`): entre la comprobación de versión y el commit no entra otra
    petición ni un ciclo. Si algo falla antes del commit, ni hay fila nueva ni cambia el holder.
    """
    at = clock()
    if config_hash(put.content) != put.hash:
        return _reject(conn, put, at, "hash_mismatch", "El hash no corresponde al contenido")
    active = control_repo.latest_version(conn)
    if active is not None:
        if put.version < active.version:
            return _reject(
                conn, put, at, "stale_version", f"La versión activa es la {active.version}"
            )
        if put.version == active.version:
            if put.hash == active.content_hash:
                return ConfigResult(result="applied", version=put.version)
            return _reject(
                conn, put, at, "version_conflict", "Esa versión ya existe con otro contenido"
            )
    forbidden = sorted(set(put.content) - CONTROLLED_SECTIONS)
    if forbidden:
        return _reject(
            conn,
            put,
            at,
            "forbidden_section",
            "Secciones no administrables desde el Hub: " + ", ".join(forbidden),
        )
    try:
        config = DdnsConfig.model_validate(overlay(holder.base_raw, put.content))
    except ValidationError as exc:
        return _reject(conn, put, at, "invalid_config", describe_errors(exc))
    try:
        require_provider_credentials(config, settings)
    except ConfigError as exc:
        return _reject(conn, put, at, "missing_credentials", str(exc))
    with transaction(conn):
        control_repo.insert_version(conn, put.version, put.hash, canonical_json(put.content), at)
        ddns_repo.insert_event(
            conn, "config_applied", at, None, {"version": put.version, "hash": put.hash}
        )
    holder.replace(config, put.version, put.hash)
    logger.info("config_applied", extra={"version": put.version})
    return ConfigResult(result="applied", version=put.version)
