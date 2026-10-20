"""Contrato `/internal/v1` de control Hub → Agent (architecture §5). Sin secretos.

Los modelos son privados: `AgentStatus` lleva la IP completa y no se expone al público.
"""

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dyndnsarr_shared.events import AgentId, EventId, Instant

COMMAND_TYPES = ("check_dns", "sync_ddns", "wake_device")
COMMAND_STATUSES = ("received", "running", "succeeded", "failed", "expired")
FINAL_COMMAND_STATUSES = ("succeeded", "failed", "expired")


def canonical_json(content: dict[str, Any]) -> str:
    """JSON de forma fija (claves ordenadas, sin espacios): lo que se hashea y se guarda."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_hash(content: dict[str, Any]) -> str:
    """`sha256` en hex del JSON canónico: no depende del orden de las claves."""
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigPut(_Model):
    """Configuración versionada. `content` es opaco para el Hub; solo el Agent la valida."""

    version: Annotated[int, Field(ge=1)]
    hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    content: dict[str, Any]


class ConfigResult(_Model):
    """`code` y `message_safe` solo con `rejected` (nunca valores de credenciales)."""

    result: Literal["applied", "rejected"]
    version: int
    code: str | None = None
    message_safe: str | None = None


class CommandRequest(_Model):
    command_id: EventId
    type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    resource_id: Annotated[str, Field(max_length=128)] | None = None
    requested_by: Annotated[str, Field(min_length=1, max_length=64)]
    expires_at: Instant
    parameters: dict[str, Any] = {}


class CommandStatus(_Model):
    """`result` lleva `code` (si falló) y detalles por registro; nunca la IP ni credenciales."""

    command_id: str
    type: str
    status: Literal["received", "running", "succeeded", "failed", "expired"]
    received_at: Instant
    finished_at: Instant | None = None
    result: dict[str, Any] | None = None


class AgentHealth(_Model):
    agent_id: AgentId | None
    version: str
    status: Literal["ok", "error"]
    time_utc: Instant
    last_local_dns_check_at: Instant | None


class RecordStatus(_Model):
    name: str
    mode: Literal["monitored", "managed"]
    verification: str
    since: Instant | None
    failures: int
    next_attempt_at: Instant | None
    last_error_code: str | None


class HubLinkStatus(_Model):
    pending_events: int
    rejected_events: int
    last_success_at: Instant | None
    last_error_code: str | None
    last_error_at: Instant | None


class AgentStatus(_Model):
    agent_id: AgentId | None
    version: str
    time_utc: Instant
    config_version: int
    config_hash: str | None
    ip_status: str | None
    public_ip: str | None
    public_ip_confirmed_at: Instant | None
    last_check_at: Instant | None
    records: list[RecordStatus]
    hub: HubLinkStatus | None


class ConfigRecord(_Model):
    name: Annotated[str, Field(max_length=253)]
    mode: Literal["monitored", "managed"]
    provider: Literal["dondominio"] | None = None
    enabled: bool = True


class ConfigSources(_Model):
    urls: Annotated[list[Annotated[str, Field(max_length=2048)]], Field(max_length=100)]


class ConfigDns(_Model):
    resolvers: Annotated[list[Annotated[str, Field(max_length=128)]], Field(max_length=100)]


class ConfigRetention(_Model):
    samples_days: int = 30
    events_days: int = 365


class ConfigUpdate(_Model):
    verify_retry_seconds: int = 60
    verify_timeout_seconds: int = 900


class ControlledConfig(_Model):
    """Forma administrable, sin validar las reglas del motor que pertenecen al Agent."""

    check_interval_seconds: int | None = None
    ip_sources: ConfigSources | None = None
    dns: ConfigDns | None = None
    retention: ConfigRetention | None = None
    update: ConfigUpdate | None = None
    records: Annotated[list[ConfigRecord], Field(max_length=1000)] | None = None


class EffectiveConfig(_Model):
    version: Annotated[int, Field(ge=0)]
    hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    content: ControlledConfig


class DiagnosticObservation(_Model):
    id: Annotated[int, Field(ge=1)]
    kind: Literal["ip", "dns"]
    observed_at: Instant
    record_name: Annotated[str, Field(max_length=253)] | None = None
    source: Annotated[str, Field(max_length=2048)]
    ips: Annotated[list[Annotated[str, Field(max_length=45)]], Field(max_length=100)] = []
    error_code: Annotated[str, Field(max_length=128)] | None = None


class RecordDiagnostic(_Model):
    name: Annotated[str, Field(max_length=253)]
    accepted_at: Instant | None
    verified_at: Instant | None


class DiagnosticPage(_Model):
    observations: Annotated[list[DiagnosticObservation], Field(max_length=100)]
    records: Annotated[list[RecordDiagnostic], Field(max_length=1000)] = []
    next_cursor: Annotated[str, Field(max_length=1024)] | None = None
