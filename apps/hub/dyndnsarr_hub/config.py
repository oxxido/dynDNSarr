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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"
