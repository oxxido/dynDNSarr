"""Política LAN local; no es parte de la configuración DDNS remota."""

import ipaddress
import tomllib
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dyndnsarr_shared.outbound import validate_url


class Pihole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    networks: list[str]
    ca_file: Path | None = None

    @model_validator(mode="after")
    def destination(self):
        validate_url(self.url, allow_http=bool(self.networks))
        if not self.networks:
            raise ValueError("Pi-hole necesita allowlist de redes")
        for network in self.networks:
            ipaddress.ip_network(network)
        return self


class WakePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
    name: Annotated[str, Field(min_length=1, max_length=253)]
    mac: Annotated[str, Field(pattern=r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")]
    ip: IPv4Address
    broadcast: IPv4Address
    port: int = Field(9, ge=1, le=65535)
    probe_port: int = Field(443, ge=1, le=65535)
    manual: bool = False
    automatic: bool = False
    cooldown_seconds: int = Field(900, ge=300, le=86400)
    boot_seconds: int = Field(180, ge=10, le=900)

    @field_validator("ip")
    @classmethod
    def local_ip(cls, value):
        if not value.is_private or value.is_loopback or value.is_link_local or value.is_unspecified:
            raise ValueError("La IP del dispositivo debe ser privada LAN")
        return value

    @field_validator("broadcast")
    @classmethod
    def local_broadcast(cls, value):
        if str(value) != "255.255.255.255" and (
            not value.is_private or value.is_loopback or value.is_link_local or value.is_unspecified
        ):
            raise ValueError("Broadcast debe ser una dirección LAN")
        return value


class IntegrationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: int = Field(60, ge=10, le=3600)
    pihole: Pihole | None = None
    devices: Annotated[list[WakePolicy], Field(max_length=32)] = []

    @field_validator("devices")
    @classmethod
    def unique_ids(cls, value):
        if len({d.id for d in value}) != len(value):
            raise ValueError("IDs de dispositivo duplicados")
        return value


def load(path):
    if path is None:
        return IntegrationConfig()
    return IntegrationConfig.model_validate(tomllib.loads(path.read_text()))
