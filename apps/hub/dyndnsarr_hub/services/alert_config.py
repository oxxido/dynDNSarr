"""Reglas opcionales locales; las credenciales y URLs secretas se reciben por entorno."""

import ipaddress
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EVENTS = {
    "ip_changed",
    "ip_uncertain",
    "ip_recovered",
    "dns_update_failed",
    "dns_update_succeeded",
    "dns_mismatch",
    "dns_verified",
    "dns_verification_failed",
    "integration_failed",
    "integration_recovered",
    "wake_completed",
}


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
    events: Annotated[list[str], Field(min_length=1, max_length=20)]
    recovery_events: Annotated[list[str], Field(max_length=20)] = []
    channels: Annotated[list[Literal["webhook", "smtp"]], Field(min_length=1, max_length=2)]
    severity: Literal["info", "warning", "critical"] = "warning"
    threshold_seconds: int = Field(0, ge=0, le=86400)
    suppress_seconds: int = Field(900, ge=60, le=604800)

    @field_validator("events", "recovery_events")
    @classmethod
    def known_events(cls, values):
        if not set(values) <= EVENTS:
            raise ValueError("Evento de alerta desconocido")
        return values

    @model_validator(mode="after")
    def disjoint(self):
        if set(self.events) & set(self.recovery_events):
            raise ValueError("Un evento no puede activar y recuperar la misma regla")
        return self


class Webhook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    networks: list[str] = []
    ca_file: Path | None = None

    @field_validator("networks")
    @classmethod
    def valid_networks(cls, values):
        for value in values:
            ipaddress.ip_network(value)
        return values


class Smtp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: Annotated[str, Field(min_length=1, max_length=253)]
    port: int = Field(465, ge=1, le=65535)
    starttls: bool = False
    networks: list[str] = []
    ca_file: Path | None = None
    sender: Annotated[str, Field(min_length=3, max_length=254)]
    recipients: Annotated[
        list[Annotated[str, Field(min_length=3, max_length=254)]],
        Field(min_length=1, max_length=10),
    ]

    @model_validator(mode="after")
    def safe_headers(self):
        if any("\n" in s or "\r" in s for s in [self.host, self.sender, *self.recipients]):
            raise ValueError("Cabecera SMTP no válida")
        for network in self.networks:
            ipaddress.ip_network(network)
        return self


class AlertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    webhook: Webhook | None = None
    smtp: Smtp | None = None
    rules: Annotated[list[Rule], Field(max_length=32)] = []

    @model_validator(mode="after")
    def valid_rules(self):
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError("IDs de regla duplicados")
        if any(getattr(self, channel) is None for rule in self.rules for channel in rule.channels):
            raise ValueError("Regla sin canal configurado")
        return self


def load(path):
    return AlertConfig.model_validate(tomllib.loads(path.read_text())) if path else AlertConfig()
