from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Settings(BaseSettings):
    """Configuración del Agent. Solo variables de entorno `DYNDNSARR_AGENT_*`."""

    model_config = SettingsConfigDict(env_prefix="DYNDNSARR_AGENT_")

    host: str = "127.0.0.1"
    port: int = Field(8001, ge=1, le=65535)
    data_dir: Path = Path("/var/lib/dyndnsarr")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # TOML del motor DDNS (sin secretos). Sin él, el motor queda desactivado.
    config_file: Path | None = None
    # Credenciales de DonDominio: solo por entorno, nunca en el TOML.
    dondominio_user: SecretStr | None = None
    dondominio_key: SecretStr | None = None
    # Token del Agent ante el Hub (solo con `[hub]` en el TOML): solo por entorno.
    hub_token: SecretStr | None = None
    # API de control del Hub (slice 3b): token Bearer del sentido Hub → Agent y TLS propio.
    api_token: SecretStr | None = None
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None

    @model_validator(mode="after")
    def _check_control_api(self) -> "Settings":
        prefix = self.model_config["env_prefix"]
        files = {"TLS_CERT_FILE": self.tls_cert_file, "TLS_KEY_FILE": self.tls_key_file}
        if (self.tls_cert_file is None) != (self.tls_key_file is None):
            raise ValueError(f"{prefix}TLS_CERT_FILE y {prefix}TLS_KEY_FILE van juntos")
        for name, path in files.items():
            if path is not None and not path.is_file():
                raise ValueError(f"{prefix}{name}: el archivo no existe")
        if self.api_enabled and not self.tls_enabled and self.host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"con {prefix}API_TOKEN y escucha fuera de loopback hace falta TLS "
                f"({prefix}TLS_CERT_FILE y {prefix}TLS_KEY_FILE): el token viajaría en claro"
            )
        return self

    @property
    def api_enabled(self) -> bool:
        return self.api_token is not None and bool(self.api_token.get_secret_value())

    @property
    def tls_enabled(self) -> bool:
        return self.tls_cert_file is not None

    @property
    def lock_path(self) -> Path:
        """Bloqueo (`flock`) que coordina el ciclo del daemon con `sync-ddns`."""
        return self.data_dir / "ddns.lock"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"
