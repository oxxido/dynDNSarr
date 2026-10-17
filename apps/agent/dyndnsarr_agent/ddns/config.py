import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from dyndnsarr_agent.config import Settings

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}


class ConfigError(Exception):
    """El archivo de configuración del motor DDNS no existe o no es válido."""


class Resolver(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str
    port: int = 53

    @property
    def label(self) -> str:
        return self.host if self.port == 53 else f"{self.host}:{self.port}"


def _parse_resolver(value: object) -> object:
    if not isinstance(value, str):
        return value
    host, port = value, 53
    try:
        ipaddress.ip_address(host)
    except ValueError:
        host, _, raw_port = value.rpartition(":")
        if not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
            raise ValueError('el resolver debe ser "ip" o "ip:puerto"') from None
        port = int(raw_port)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError('el resolver debe ser "ip" o "ip:puerto"') from None
    return {"host": host, "port": port}


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IpSources(_Section):
    urls: list[str]

    @field_validator("urls")
    @classmethod
    def _check_urls(cls, urls: list[str]) -> list[str]:
        if len(urls) < 2:
            raise ValueError("se necesitan al menos 2 fuentes independientes")
        if len(set(urls)) != len(urls):
            raise ValueError("hay fuentes repetidas")
        for url in urls:
            parts = urlsplit(url)
            loopback_http = parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS
            if parts.scheme != "https" and not loopback_http:
                raise ValueError(f"{url!r}: solo https (http únicamente para localhost/127.0.0.1)")
            if not parts.hostname or parts.username is not None:
                raise ValueError(f"{url!r}: URL no válida (sin credenciales en la URL)")
        return urls


class Dns(_Section):
    resolvers: list[Annotated[Resolver, BeforeValidator(_parse_resolver)]]

    @field_validator("resolvers")
    @classmethod
    def _check_resolvers(cls, resolvers: list[Resolver]) -> list[Resolver]:
        if len(resolvers) < 1:
            raise ValueError("se necesita al menos 1 resolver")
        if len(set(resolvers)) != len(resolvers):
            raise ValueError("hay resolvers repetidos")
        return resolvers


class Retention(_Section):
    samples_days: int = Field(30, ge=1)
    events_days: int = Field(365, ge=1)


class Update(_Section):
    verify_retry_seconds: int = Field(60, ge=1)
    verify_timeout_seconds: int = Field(900, ge=1)

    @model_validator(mode="after")
    def _check_timeout(self) -> "Update":
        if self.verify_timeout_seconds < self.verify_retry_seconds:
            raise ValueError("verify_timeout_seconds no puede ser menor que verify_retry_seconds")
        return self


class DonDominio(_Section):
    url: str = "https://dondns.dondominio.com/json/"

    @field_validator("url")
    @classmethod
    def _check_url(cls, url: str) -> str:
        parts = urlsplit(url)
        loopback_http = parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS
        if parts.scheme != "https" and not loopback_http:
            raise ValueError("solo https (http únicamente para localhost/127.0.0.1)")
        if not parts.hostname or parts.username is not None:
            raise ValueError("URL no válida (sin credenciales en la URL)")
        return url


class Providers(_Section):
    dondominio: DonDominio = DonDominio()


class Record(_Section):
    name: str
    mode: Literal["monitored", "managed"]
    provider: Literal["dondominio"] | None = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _check_name(cls, name: str) -> str:
        name = name.strip().rstrip(".").lower()
        if not _HOSTNAME.match(name):
            raise ValueError("no es un nombre de host válido")
        return name

    @model_validator(mode="after")
    def _check_provider(self) -> "Record":
        if self.mode == "managed" and self.provider is None:
            raise ValueError('un registro "managed" necesita provider = "dondominio"')
        if self.mode == "monitored" and self.provider is not None:
            raise ValueError('un registro "monitored" no usa provider: nunca se escribe')
        return self


class DdnsConfig(_Section):
    check_interval_seconds: int = Field(300, ge=60)
    ip_sources: IpSources
    dns: Dns
    retention: Retention = Retention()
    update: Update = Update()
    providers: Providers = Providers()
    records: list[Record] = []

    @field_validator("records")
    @classmethod
    def _check_records(cls, records: list[Record]) -> list[Record]:
        names = [r.name for r in records]
        if len(set(names)) != len(names):
            raise ValueError("hay registros repetidos")
        return records


def require_provider_credentials(config: DdnsConfig, settings: Settings) -> None:
    """Un registro `managed` habilitado necesita las credenciales del proveedor (por entorno)."""
    if not any(r.mode == "managed" and r.enabled for r in config.records):
        return
    missing = [
        f"{Settings.model_config['env_prefix']}{name.upper()}"
        for name, secret in (
            ("dondominio_user", settings.dondominio_user),
            ("dondominio_key", settings.dondominio_key),
        )
        if secret is None or not secret.get_secret_value()
    ]
    if missing:
        raise ConfigError(
            "hay registros managed habilitados y faltan las credenciales de DonDominio: "
            + ", ".join(missing)
        )


def load_ddns_config(path: Path) -> DdnsConfig:
    """Lee y valida el TOML del motor DDNS. Lanza `ConfigError` con un mensaje legible."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"el archivo de configuración no existe: {path}") from None
    except OSError as exc:
        raise ConfigError(f"no se puede leer {path}: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML inválido: {exc}") from None
    try:
        return DdnsConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False)
        )
        raise ConfigError(f"{path}: configuración inválida:\n{problems}") from None
