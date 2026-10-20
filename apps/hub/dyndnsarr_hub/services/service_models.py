"""Catálogo compartido por API y MCP. Sin objetos JSON libres ni destinos remotos libres."""

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dyndnsarr_shared.events import AgentId, Instant
from dyndnsarr_shared.outbound import allowed_address, validate_url

Id = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")]
Domain = Annotated[str, Field(max_length=253)]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]


def domain(value):
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value) or "." not in value:
        raise ValueError("Dominio DNS exacto en minúsculas requerido")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", s) for s in value.split(".")):
        raise ValueError("Etiqueta DNS no válida")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("Se requiere un dominio, no una IP")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentQuery(Model):
    agent_id: AgentId


class HistoryQuery(AgentQuery):
    domain: Domain
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    cursor: Annotated[str, Field(max_length=2048)] | None = None

    _domain = field_validator("domain")(domain)


class DnsAction(AgentQuery):
    operation_id: UUID
    domain: Domain

    _domain = field_validator("domain")(domain)


class WakeAction(AgentQuery):
    operation_id: UUID
    device_id: Id


class OperationQuery(Model):
    operation_id: UUID


class Empty(Model):
    pass


class PublicationQuery(Model):
    publication_id: Id


class Publish(PublicationQuery):
    operation_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    domain: Domain
    destination: Id
    port: Port
    access: Literal["public", "private"] = "public"
    enabled: bool = True
    expires_at: Instant | None = None

    _domain = field_validator("domain")(domain)

    @field_validator("expires_at")
    @classmethod
    def canonical_expiry(cls, value):
        if value is not None:
            from dyndnsarr_hub.clock import iso, parse_instant

            if iso(parse_instant(value)) != value:
                raise ValueError("Expiración UTC canónica requerida")
        return value


class ChangePublication(PublicationQuery):
    operation_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class Rollback(ChangePublication):
    revision: Annotated[int, Field(strict=True, ge=1)]


TOOLS = {
    "get_status": (AgentQuery, "read", "Consulta conectividad observada del Agent autorizado."),
    "list_domains": (AgentQuery, "read", "Lista registros DNS autorizados y su estado."),
    "dns_history": (HistoryQuery, "read", "Consulta historial DNS paginado de un dominio."),
    "list_devices": (AgentQuery, "read", "Consulta dispositivos autorizados observados."),
    "get_operation": (OperationQuery, "read", "Consulta una operación propia sin reejecutarla."),
    "check_dns": (DnsAction, "operate", "Comprueba únicamente el registro DNS indicado."),
    "sync_ddns": (DnsAction, "operate", "Solicita DDNS de un registro managed autorizado."),
    "wake_device": (WakeAction, "operate", "Solicita WoL de un dispositivo autorizado."),
    "list_publications": (Empty, "read", "Lista publicaciones autorizadas y revisiones."),
    "publication_history": (PublicationQuery, "read", "Consulta revisiones de una publicación."),
    "verify_publication": (PublicationQuery, "operate", "Observa accesibilidad del destino."),
    "publish": (Publish, "publish", "Crea o modifica una publicación HTTPS autorizada."),
    "disable_publication": (ChangePublication, "publish", "Deshabilita una publicación."),
    "delete_publication": (ChangePublication, "publish", "Retira una publicación."),
    "rollback_publication": (Rollback, "publish", "Recupera una revisión con permisos actuales."),
}


class Grants(Model):
    actions: Annotated[list[Literal[tuple(TOOLS)]], Field(max_length=32)]
    agents: Annotated[list[AgentId], Field(max_length=32)] = []
    domains: Annotated[list[Domain], Field(max_length=100)] = []
    devices: Annotated[list[Id], Field(max_length=100)] = []
    destinations: Annotated[list[Id], Field(max_length=100)] = []
    ports: Annotated[list[Port], Field(max_length=100)] = []

    @field_validator("domains")
    @classmethod
    def domains_exact(cls, values):
        return [domain(value) for value in values]


class Destination(Model):
    host: str
    ports: Annotated[list[Port], Field(min_length=1, max_length=100)]
    scheme: Literal["http", "https"] = "http"

    @field_validator("host")
    @classmethod
    def literal_address(cls, value):
        ip = ipaddress.IPv4Address(value)
        return allowed_address(str(ip), [str(ip) + "/32"])


class GatewayPolicy(Model):
    admin_url: str
    admin_networks: Annotated[list[str], Field(max_length=32)] = []
    server: Id
    domains: Annotated[list[Domain], Field(min_length=1, max_length=100)]
    destinations: Annotated[dict[Id, Destination], Field(min_length=1, max_length=100)]
    private_networks: Annotated[list[str], Field(max_length=32)] = []

    @field_validator("domains")
    @classmethod
    def exact_domains(cls, values):
        return [domain(value) for value in values]

    @field_validator("admin_networks", "private_networks")
    @classmethod
    def networks(cls, values):
        return [str(ipaddress.ip_network(v, strict=True)) for v in values]

    @model_validator(mode="after")
    def admin_endpoint(self):
        validate_url(self.admin_url, allow_http=True)
        p = urlsplit(self.admin_url)
        if p.path not in ("", "/") or p.query:
            raise ValueError("Admin URL debe ser un origen sin ruta")
        if p.scheme == "http" and p.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Admin URL requiere HTTPS fuera de loopback")
        self.admin_url = self.admin_url.rstrip("/")
        return self


def load_gateway(path: Path | None):
    if path is None:
        return None
    return GatewayPolicy.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
