from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del Agent. Solo variables de entorno `DYNDNSARR_AGENT_*`."""

    model_config = SettingsConfigDict(env_prefix="DYNDNSARR_AGENT_")

    host: str = "127.0.0.1"
    port: int = Field(8001, ge=1, le=65535)
    data_dir: Path = Path("/var/lib/dyndnsarr")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # TOML del motor DDNS (sin secretos). Sin él, el motor queda desactivado.
    config_file: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"
