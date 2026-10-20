"""Snapshots privados acotados; nunca se reutilizan en la proyección pública."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress

from dyndnsarr_shared.events import Instant


class Device(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(max_length=128)]
    name: Annotated[str, Field(max_length=253)]
    ip: IPvAnyAddress
    mac: Annotated[str, Field(max_length=32)] | None = None
    state: Literal["active", "likely_active", "known", "stale", "unknown"]
    last_activity_at: Instant | None = None
    lease_expires_at: Instant | None = None
    manual: bool = False
    automatic: bool = False
    wake_state: Literal["sent", "booting", "reachable", "unconfirmed"] | None = None


class IntegrationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: Instant
    last_success_at: Instant | None = None
    pihole: Literal["disabled", "ok", "unavailable"]
    error_code: Literal["integration_failed"] | None = None
    devices: Annotated[list[Device], Field(max_length=500)] = []
    targets: Annotated[list[Device], Field(max_length=32)] = []
