"""Resumen Homarr: solo proyección pública; no hace peticiones a Homarr."""

from pydantic import BaseModel, ConfigDict

from dyndnsarr_hub.public.builder import build_dto
from dyndnsarr_hub.public.dto import State
from dyndnsarr_shared.events import Instant


class HomarrStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: State
    last_successful_check_at: Instant | None
    last_ip_change_at: Instant | None
    application_url: str


def build(conn, now, settings):
    dto = build_dto(conn, now, settings)
    last = conn.execute(
        "SELECT max(p.occurred_at) FROM public_ip_changes p JOIN agents a "
        "USING(agent_id) WHERE a.disabled=0"
    ).fetchone()[0]
    return HomarrStatus(
        state=dto.state,
        last_successful_check_at=dto.last_successful_check_at,
        last_ip_change_at=last,
        application_url=settings.application_url or "/",
    )
