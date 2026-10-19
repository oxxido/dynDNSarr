"""Configuración activa: el TOML local con las secciones de la versión remota por encima."""

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.ddns.config import (
    ConfigError,
    DdnsConfig,
    read_toml,
    validate_ddns_config,
)
from dyndnsarr_agent.dns.resolver import DnsObserver
from dyndnsarr_agent.providers.base import DnsProvider

logger = logging.getLogger(__name__)

# Lo que el Hub puede administrar. El resto (`providers`, `hub`) es local: ver ADR-0006.
CONTROLLED_SECTIONS = frozenset(
    {"check_interval_seconds", "ip_sources", "dns", "retention", "update", "records"}
)

RESET_HINT = "usa `dyndnsarr-agent config-reset` para volver a la configuración del TOML"


@dataclass(frozen=True)
class ActiveConfig:
    """`base_raw` es el TOML tal como se leyó; `version` 0 significa que manda el TOML."""

    config: DdnsConfig
    base_raw: dict[str, Any]
    version: int = 0
    content_hash: str | None = None


def overlay(base_raw: Mapping[str, Any], content: Mapping[str, Any]) -> dict[str, Any]:
    """Las secciones de `content` sustituyen a las del TOML; las demás vienen del TOML."""
    return {**base_raw, **content}


def _stored_version(settings: Settings) -> tuple[int, str, dict[str, Any]] | None:
    """Versión remota activa leída en solo lectura (`None` si no hay BD, tabla o filas)."""
    if not settings.db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT version, content_hash, content FROM configuration_versions "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:  # BD anterior a la migración 0005
        return None
    finally:
        conn.close()
    return (row[0], row[1], json.loads(row[2])) if row else None


def load_active(settings: Settings) -> ActiveConfig:
    """Carga el TOML y, si hay, la versión remota activa. Lanza `ConfigError` si algo no vale.

    El TOML se valida siempre (también aporta las secciones locales). Una versión guardada que ya
    no valida (p. ej. cambió el TOML) impide cargar y nombra `config-reset`.
    """
    path = settings.config_file
    if path is None:
        raise ConfigError("falta DYNDNSARR_AGENT_CONFIG_FILE")
    base_raw = read_toml(path)
    base = validate_ddns_config(base_raw, str(path))
    stored = _stored_version(settings)
    if stored is None:
        return ActiveConfig(base, base_raw)
    version, content_hash, content = stored
    try:
        config = validate_ddns_config(
            overlay(base_raw, content), f"configuración remota (versión {version})"
        )
    except ConfigError as exc:
        raise ConfigError(f"{exc}\nLa versión activa ya no es válida: {RESET_HINT}") from None
    overridden = sorted(s for s, value in content.items() if base_raw.get(s) != value)
    if overridden:
        logger.warning("config_overrides_toml", extra={"version": version, "sections": overridden})
    return ActiveConfig(config, base_raw, version, content_hash)


Rebuild = Callable[[DdnsConfig], tuple[Mapping[str, DnsProvider], DnsObserver]]


class ConfigHolder:
    """Configuración, proveedores y observador vigentes. Un ciclo toma una referencia al empezar
    y la conserva: un cambio solo lo recoge el siguiente (no hay cambios a mitad de ciclo).

    `rebuild` reconstruye proveedores y observador para una configuración nueva (un registro
    `managed` añadido necesita su proveedor; los resolvers pueden cambiar).
    """

    def __init__(
        self,
        config: DdnsConfig,
        providers: Mapping[str, DnsProvider],
        observer: DnsObserver,
        *,
        base_raw: dict[str, Any] | None = None,
        version: int = 0,
        content_hash: str | None = None,
        rebuild: Rebuild | None = None,
    ) -> None:
        self.config = config
        self.providers = providers
        self.observer = observer
        self.base_raw = base_raw or {}
        self.version = version
        self.content_hash = content_hash
        self._rebuild = rebuild

    def replace(self, config: DdnsConfig, version: int, content_hash: str | None) -> None:
        # Se reconstruye antes de asignar nada: si falla, el holder queda como estaba.
        if self._rebuild is not None:
            providers, observer = self._rebuild(config)
            self.providers, self.observer = providers, observer
        self.config, self.version, self.content_hash = config, version, content_hash
