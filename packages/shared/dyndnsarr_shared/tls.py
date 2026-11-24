"""TLS verificado para los clientes httpx de Agent y Hub."""

import ssl
from pathlib import Path


def build_ssl_context(ca_file: Path | None, ca_pem: str | None = None) -> ssl.SSLContext:
    """Contexto con verificación completa: solo la CA indicada (en PEM o en archivo), o el
    almacén del sistema. `ca_pem` (CA fijada al emparejar, ADR-0013) manda sobre `ca_file`.

    No hay forma de desactivar la verificación.
    """
    if ca_pem is not None:
        return ssl.create_default_context(cadata=ca_pem)
    return ssl.create_default_context(cafile=str(ca_file) if ca_file else None)


def unverified_bootstrap_context() -> ssl.SSLContext:
    """Solo para el desafío del emparejamiento (ADR-0013): aún no hay CA que verificar y no viaja
    ningún secreto. Lo que se recibe así vale únicamente si su HMAC con el código cuadra."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def has_tls_error(exc: BaseException | None) -> bool:
    """¿Alguna excepción de la cadena `__cause__`/`__context__` es un error TLS?"""
    while exc is not None:
        if isinstance(exc, ssl.SSLError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False
