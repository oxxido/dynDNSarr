import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator

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


class Record(_Section):
    name: str
    mode: Literal["monitored"]
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _check_name(cls, name: str) -> str:
        name = name.strip().rstrip(".").lower()
        if not _HOSTNAME.match(name):
            raise ValueError("no es un nombre de host válido")
        return name

    @field_validator("mode", mode="before")
    @classmethod
    def _check_mode(cls, mode: object) -> object:
        if mode == "managed":
            raise ValueError('mode = "managed" aún no está disponible: llega en el slice 2b')
        if mode != "monitored":
            raise ValueError('debe ser "monitored"')
        return mode


class DdnsConfig(_Section):
    check_interval_seconds: int = Field(300, ge=60)
    ip_sources: IpSources
    dns: Dns
    retention: Retention = Retention()
    records: list[Record] = []

    @field_validator("records")
    @classmethod
    def _check_records(cls, records: list[Record]) -> list[Record]:
        names = [r.name for r in records]
        if len(set(names)) != len(names):
            raise ValueError("hay registros repetidos")
        return records


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
