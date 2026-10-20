from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del Hub. Solo variables de entorno `DYNDNSARR_HUB_*`."""

    model_config = SettingsConfigDict(env_prefix="DYNDNSARR_HUB_")

    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    data_dir: Path = Path("/data")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # CA que firma el certificado de los Agents (solo esa CA; sin ella, el almacén del sistema).
    agent_ca_file: Path | None = None

    # Landing pública (slice 4, ADR-0007). Con `public_enabled = false`, `/`, `/public/*` y
    # `/static/*` dan 404. `public_show_domains` añade el nombre del registro a las renovaciones:
    # ese nombre resuelve a la IP completa en el DNS público.
    public_enabled: bool = True
    public_show_domains: bool = False
    # Cada cuánto consulta el Hub el estado de cada Agent con API configurada.
    status_refresh_seconds: int = Field(60, ge=15)

    # Origen HTTPS que presenta el navegador; el Hub puede estar detrás de un proxy HTTP.
    admin_origin: str | None = None

    @field_validator("admin_origin")
    @classmethod
    def _admin_origin(cls, value):
        from urllib.parse import urlsplit

        if value in (None, ""):
            return None
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("ADMIN_ORIGIN debe ser un origen HTTPS sin ruta ni credenciales")
        return value.rstrip("/")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"
