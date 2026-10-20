from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del Hub. Solo variables de entorno `DYNDNSARR_HUB_*`."""

    model_config = SettingsConfigDict(env_prefix="DYNDNSARR_HUB_")

    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    data_dir: Path = Path("/data")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # CA que firma el certificado de los Agents (solo esa CA; sin ella, el almacén del sistema).
    agent_ca_file: Path | None = None

    # Landing pública (slice 4, ADR-0007). Con `public_enabled = false`, `/`, `/public/*` y
    # `/static/*` dan 404. `public_show_domains` añade el nombre del registro a las renovaciones:
    # ese nombre resuelve a la IP completa en el DNS público.
    public_enabled: bool = True
    public_show_domains: bool = False
    # Cada cuánto consulta el Hub el estado de cada Agent con API configurada.
    status_refresh_seconds: int = Field(60, ge=15)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"
