from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del Hub. Solo variables de entorno `DYNDNSARR_HUB_*`."""

    model_config = SettingsConfigDict(env_prefix="DYNDNSARR_HUB_")

    services_enabled: bool = False
    gateway_file: Path | None = None
    gateway_ca_file: Path | None = None
    gateway_token: SecretStr | None = None
    mcp_hub_url: str = "https://localhost"
    mcp_token: SecretStr | None = None
    mcp_ca_file: Path | None = None

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
    alerts_file: Path | None = None
    integrations_enabled: bool = False
    webhook_url: SecretStr | None = None
    smtp_user: SecretStr | None = None
    smtp_password: SecretStr | None = None
    retention_days: int = Field(365, ge=1, le=3650)
    receipts_days: int = Field(400, ge=1, le=3650)
    homarr_origin: str | None = None
    application_url: str | None = None

    @field_validator("alerts_file", "gateway_file", "gateway_ca_file", "mcp_ca_file", mode="before")
    @classmethod
    def optional_alert_file(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def retention_window(self):
        if self.receipts_days < self.retention_days:
            raise ValueError("RECEIPTS_DAYS debe ser al menos RETENTION_DAYS")
        return self

    @field_validator("application_url")
    @classmethod
    def application_destination(cls, value):
        if not value:
            return None
        from dyndnsarr_shared.outbound import validate_url

        return validate_url(value)

    @field_validator("admin_origin", "homarr_origin")
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
