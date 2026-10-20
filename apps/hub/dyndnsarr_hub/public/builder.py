"""Construye el `PublicStatusDTO` a partir de la proyección pública (ADR-0007).

Solo lee las tablas `public_*` (vía `public_repo`), nunca `events`: aunque un fallo de filtrado
ocurriera, la IP completa no está en lo que se lee.
"""

import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from dyndnsarr_hub.clock import iso
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import public_repo
from dyndnsarr_hub.public.dto import (
    MAX_RENEWALS,
    Check,
    History,
    PublicStatusDTO,
    Renewal,
)
from dyndnsarr_hub.public.state import (
    AgentProjection,
    build_buckets,
    derive_state,
    history_start,
    worst,
)

Level = Literal["ok", "attention", "unknown"]


def _worst_level(levels: Iterable[Level]) -> Level:
    """`attention` > `unknown` > `ok`; sin agentes, `unknown`."""
    present = set(levels)
    return next((v for v in ("attention", "unknown", "ok") if v in present), "unknown")


def _dns(projection: AgentProjection, known: bool) -> Level:
    if not known:
        return "unknown"
    attention = any(r.verification != "verified" or r.failures > 0 for r in projection.records)
    return "attention" if attention else "ok"


def _ipv4(projection: AgentProjection, known: bool) -> Level:
    if not known or projection.ip_status not in ("confirmed", "uncertain"):
        return "unknown"
    return "ok" if projection.ip_status == "confirmed" else "attention"


def build_dto(conn: sqlite3.Connection, now: datetime, settings: Settings) -> PublicStatusDTO:
    refresh = settings.status_refresh_seconds
    statuses = public_repo.enabled_agent_statuses(conn)
    states = [derive_state(p, now, refresh, public_repo.monitoring_policy(conn)) for p in statuses]
    dns: list[Level] = []
    ipv4: list[Level] = []
    service: list[Level] = []
    for projection, state in zip(statuses, states, strict=True):
        # Un agente sin datos recientes (`unknown`) no aporta un valor que no se observó.
        known = projection is not None and state != "unknown"
        dns.append(_dns(projection, known) if projection else "unknown")
        ipv4.append(_ipv4(projection, known) if projection else "unknown")
        service.append("ok" if known else "unknown")
    confirmed = [p.public_ip_confirmed_at for p in statuses if p and p.public_ip_confirmed_at]

    dns_level = _worst_level(dns)
    renewals = [
        Renewal(
            occurred_at=iso(r.occurred_at),
            ip_suffix=r.ip_suffix,
            verification="verified" if r.verified else "pending",
            record=r.record_name if settings.public_show_domains else None,
        )
        for r in public_repo.recent_renewals(conn, MAX_RENEWALS)
    ]
    start = history_start(now)
    buckets, coverage = build_buckets(public_repo.samples_since(conn, start), now)
    return PublicStatusDTO(
        generated_at=iso(now),
        state=worst(states),
        last_successful_check_at=iso(max(confirmed)) if confirmed else None,
        dns=dns_level,
        checks=[
            Check(id="dns_resolution", state=dns_level),
            Check(id="ipv4", state=_worst_level(ipv4)),
            Check(id="ipv6", state="unavailable"),
            Check(id="service", state=_worst_level(service)),
        ],
        renewals=renewals,
        history=History.model_validate(
            {"from": iso(start), "buckets": buckets, "coverage": coverage}
        ),
    )
