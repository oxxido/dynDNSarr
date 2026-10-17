from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class UpdateResult:
    """Resultado de pedir al proveedor que apunte un registro a una IP.

    `error_code` es siempre un código seguro (nunca el texto de una excepción ni la URL).
    """

    accepted: bool
    changed: bool = False
    error_code: str | None = None
    retryable: bool = True


class DnsProvider(Protocol):
    async def update(self, host: str, ip: str) -> UpdateResult:
        """Apunta el registro A `host` a `ip`. Un fallo es un resultado, no una excepción."""
        ...
