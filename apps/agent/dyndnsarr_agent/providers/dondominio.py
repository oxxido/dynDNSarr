import asyncio

import httpx
from pydantic import SecretStr

from dyndnsarr_agent.providers.base import UpdateResult

# Los errores llegan con HTTP 200 y se distinguen por el texto exacto (`lang=en` fijo). Solo se
# bloquea lo visto en respuestas reales; cualquier otro texto es `rejected` y reintentable.
_DEFINITIVE_MESSAGES = {
    "Wrong key": "auth_failed",
    "Incorrect data": "auth_failed",
    "The DNS Zone doesn't exist": "host_not_found",
    "Domain not found": "host_not_found",
}


def _failure(code: str, retryable: bool = True) -> UpdateResult:
    return UpdateResult(False, error_code=code, retryable=retryable)


def _classify_failure(body: dict) -> UpdateResult:
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            code = _DEFINITIVE_MESSAGES.get(message) if isinstance(message, str) else None
            if code:
                return _failure(code, retryable=False)
    return _failure("rejected")


class DonDominioProvider:
    """DonDNS (`GET .../json/`): una llamada por actualización, con la IP siempre explícita.

    La clave viaja en la URL (el API es GET): nada de aquí registra la petición, y ningún error
    propaga el texto de una excepción (podría incluirla), solo códigos seguros.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        user: SecretStr,
        key: SecretStr,
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._url = url
        self._user = user
        self._key = key
        self._timeout = timeout

    async def update(self, host: str, ip: str) -> UpdateResult:
        params = {
            "user": self._user.get_secret_value(),
            "password": self._key.get_secret_value(),
            "host": host,
            "ip": ip,
            "lang": "en",
        }
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.get(self._url, params=params)
        except (TimeoutError, httpx.TimeoutException):
            return _failure("timeout")
        except httpx.ConnectError:
            return _failure("connect_error")
        except httpx.HTTPError:
            return _failure("http_error")
        if response.status_code == 429:
            return _failure("rate_limited")
        if response.status_code != 200:
            return _failure("http_error")
        try:
            body = response.json()
        except ValueError:
            return _failure("bad_response")
        if not isinstance(body, dict) or not isinstance(body.get("success"), bool):
            return _failure("bad_response")
        if not body["success"]:
            return _classify_failure(body)
        data = body.get("host")
        if not isinstance(data, dict) or data.get("value") != ip:
            return _failure("bad_response")
        return UpdateResult(True, changed=data.get("updated") is True)
