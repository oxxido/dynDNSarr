import ipaddress
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dyndnsarr_shared.pairing import normalize_code

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


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
    # Un Agent emparejado usa la CA que entregó al emparejar (ADR-0013).
    agent_ca_file: Path | None = None

    # Puerto de enlace HTTPS propio para los Agents (ADR-0013): IPs/nombres LAN del servidor,
    # separados por comas, que van al certificado. Vacío: sin puerto de enlace.
    link_names: str = ""
    link_port: int = Field(8443, ge=1, le=65535)
    # Código de emparejamiento de un solo uso (≥128 bits en hex), el mismo que en el Agent.
    pairing_code: SecretStr | None = None

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

    @field_validator("pairing_code", mode="before")
    @classmethod
    def _pairing_code(cls, value):
        if value in (None, ""):
            return None
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        return SecretStr(normalize_code(value))

    @field_validator("link_names")
    @classmethod
    def _link_names(cls, value: str) -> str:
        names = [n.strip().lower() for n in value.split(",") if n.strip()]
        for name in names:
            try:
                ipaddress.ip_address(name)
            except ValueError:
                if not _HOSTNAME.match(name):
                    raise ValueError(f"{name!r} no es una IP ni un nombre de host") from None
        return ",".join(names)

    @model_validator(mode="after")
    def _pairing_needs_link(self):
        if self.pairing_code is not None and not self.link_enabled:
            prefix = self.model_config["env_prefix"]
            raise ValueError(f"{prefix}PAIRING_CODE necesita {prefix}LINK_NAMES")
        return self

    @property
    def link_enabled(self) -> bool:
        return bool(self.link_names)

    @property
    def link_name_list(self) -> list[str]:
        return self.link_names.split(",") if self.link_names else []

    @property
    def pairing_code_value(self) -> str:
        return self.pairing_code.get_secret_value() if self.pairing_code else ""

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
