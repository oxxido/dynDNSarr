"""`PublicStatusDTO`: lo único que sale por la vista pública (ADR-0007, D2).

Es una lista blanca: `extra="forbid"`, sin campos libres ni `dict[str, Any]`. Lo que no está
declarado aquí no se puede emitir; en concreto no hay IP completa, solo `ip_suffix`.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from dyndnsarr_shared.events import Instant

State = Literal["healthy", "degraded", "critical", "unknown"]
CheckState = Literal["ok", "attention", "unavailable", "unknown"]

MAX_RENEWALS = 5
HISTORY_BUCKETS = 84
BUCKET_SECONDS = 7200


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Check(_Model):
    id: Literal["dns_resolution", "ipv4", "ipv6", "service"]
    state: CheckState


class Renewal(_Model):
    occurred_at: Instant
    # Los dos últimos dígitos de la IPv4 (último octeto): una IP completa no valida.
    ip_suffix: Annotated[str, Field(pattern=r"^[0-9]{1,2}$")]
    verification: Literal["verified", "pending"]
    # Solo con DYNDNSARR_HUB_PUBLIC_SHOW_DOMAINS; si no, `None`.
    record: str | None = None


class History(_Model):
    from_: Annotated[Instant, Field(alias="from")]
    bucket_seconds: Literal[7200] = BUCKET_SECONDS
    buckets: Annotated[list[State], Field(min_length=HISTORY_BUCKETS, max_length=HISTORY_BUCKETS)]
    # Fracción de barras con alguna observación: no se afirma un uptime que no se observó (§8).
    coverage: Annotated[float, Field(ge=0, le=1)]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PublicStatusDTO(_Model):
    schema_version: Literal[1] = 1
    generated_at: Instant
    state: State
    last_successful_check_at: Instant | None
    dns: Literal["ok", "attention", "unknown"]
    checks: list[Check]
    renewals: Annotated[list[Renewal], Field(max_length=MAX_RENEWALS)]
    history: History
