"""Cliente del Hub hacia la API de control del Agent (`/internal/v1`, ADR-0006)."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from dyndnsarr_shared.control import (
    AgentHealth,
    AgentStatus,
    CommandRequest,
    CommandStatus,
    ConfigPut,
    ConfigResult,
    DiagnosticPage,
    EffectiveConfig,
)
from dyndnsarr_shared.events import ErrorResponse
from dyndnsarr_shared.integrations import IntegrationSnapshot
from dyndnsarr_shared.tls import build_ssl_context, has_tls_error

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}

_SAFE_CODES = frozenset(
    {
        "timeout",
        "connect_error",
        "tls_error",
        "auth_failed",
        "server_error",
        "bad_response",
        "tls_config",
        "invalid_url",
        "no_status",
        "not_found",
        "invalid_request",
        "body_too_large",
        "unsupported_command",
        "invalid_command",
        "command_conflict",
        "queue_full",
        "busy",
        "interrupted",
        "internal_error",
        "expired",
        "ip_uncertain",
        "provider_failed",
        "hash_mismatch",
        "stale_version",
        "version_conflict",
        "forbidden_section",
        "invalid_config",
        "missing_credentials",
        "no_managed_records",
        "invalid_query",
        "invalid_cursor",
        "device_not_allowed",
        "cooldown",
    }
)


def safe_code(value):
    return (
        value
        if isinstance(value, str) and value in _SAFE_CODES
        else ("remote_error" if value is not None else None)
    )


T = TypeVar("T", bound=BaseModel)


def check_api_url(url: str) -> str:
    """URL base de la API del Agent: `https`, o `http` solo a loopback (pruebas). Sin credenciales,
    parámetros ni fragmento. Devuelve la URL sin la barra final o lanza `ValueError`."""
    parts = urlsplit(url)
    loopback_http = parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS
    if parts.scheme != "https" and not loopback_http:
        raise ValueError("solo https (http únicamente para localhost/127.0.0.1)")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("URL no válida (sin credenciales en la URL)")
    if parts.query or parts.fragment:
        raise ValueError("la URL no admite parámetros ni fragmento")
    return url.rstrip("/")


def build_http_client(
    ca_file: Path | None, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """Cliente con verificación TLS completa: solo la CA indicada, o el almacén del sistema.

    Sin conexiones persistentes: el refresco de estado (slice 4) llama una vez por minuto y una
    conexión TLS ociosa en el pool no contesta al cierre del Agent, que tardaría hasta 30 s en
    apagarse."""
    return httpx.AsyncClient(
        verify=build_ssl_context(ca_file),
        transport=transport,
        limits=httpx.Limits(max_keepalive_connections=0),
    )


@dataclass(frozen=True)
class Reply(Generic[T]):
    """Respuesta de una llamada. Con `error_code` no hay respuesta útil del Agent (códigos
    seguros: nunca el texto de una excepción ni la URL); `api_error` es el `code` con el que el
    Agent rechazó la petición (p. ej. `queue_full`)."""

    value: T | None = None
    error_code: str | None = None
    http_status: int | None = None
    api_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


class AgentClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, token: str) -> None:
        self._http = http
        self._base = check_api_url(base_url) + "/internal/v1"
        self._token = token

    async def health(self) -> Reply[AgentHealth]:
        return await self._call("GET", "/health", AgentHealth)

    async def status(self) -> Reply[AgentStatus]:
        return await self._call("GET", "/status", AgentStatus)

    async def config(self) -> Reply[EffectiveConfig]:
        return await self._call("GET", "/config", EffectiveConfig)

    async def integrations(self) -> Reply[IntegrationSnapshot]:
        return await self._call("GET", "/integrations", IntegrationSnapshot)

    async def diagnostics(self, cursor: str | None = None) -> Reply[DiagnosticPage]:
        from urllib.parse import urlencode

        query = "?" + urlencode({"cursor": cursor}) if cursor else ""
        return await self._call("GET", "/diagnostics" + query, DiagnosticPage)

    async def put_config(self, put: ConfigPut) -> Reply[ConfigResult]:
        return await self._call("PUT", "/config", ConfigResult, put)

    async def post_command(self, request: CommandRequest) -> Reply[CommandStatus]:
        return await self._call("POST", "/commands", CommandStatus, request)

    async def command(self, command_id: str) -> Reply[CommandStatus]:
        return await self._call("GET", f"/commands/{command_id}", CommandStatus)

    async def _call(
        self, method: str, path: str, model: type[T], body: BaseModel | None = None
    ) -> Reply[T]:
        headers = {"Authorization": f"Bearer {self._token}"}
        content = None
        if body is not None:
            content = body.model_dump_json()
            headers["Content-Type"] = "application/json"
        try:
            response = await self._http.request(
                method, self._base + path, headers=headers, content=content, timeout=TIMEOUT_SECONDS
            )
        except httpx.TimeoutException:
            return Reply(error_code="timeout")
        except httpx.TransportError as exc:
            return Reply(error_code="tls_error" if has_tls_error(exc) else "connect_error")
        status = response.status_code
        if status in (401, 403):
            return Reply(error_code="auth_failed", http_status=status)
        if status >= 500:
            return Reply(error_code="server_error", http_status=status)
        try:
            if 200 <= status < 300:
                return Reply(value=model.model_validate_json(response.content), http_status=status)
            error = ErrorResponse.model_validate_json(response.content)
        except ValidationError:
            return Reply(error_code="bad_response", http_status=status)
        return Reply(http_status=status, api_error=safe_code(error.code))
