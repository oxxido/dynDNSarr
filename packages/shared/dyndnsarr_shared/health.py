from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    """Respuesta de `GET /healthz`. No expone datos internos (rutas, contadores, configuración)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "error"]
    component: Literal["agent", "hub"]
    version: str
