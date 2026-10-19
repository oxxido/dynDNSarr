"""TLS verificado para los clientes httpx de Agent y Hub."""

import ssl
from pathlib import Path


def build_ssl_context(ca_file: Path | None) -> ssl.SSLContext:
    """Contexto con verificación completa: solo la CA indicada, o el almacén del sistema.

    No hay forma de desactivar la verificación.
    """
    return ssl.create_default_context(cafile=str(ca_file) if ca_file else None)


def has_tls_error(exc: BaseException | None) -> bool:
    """¿Alguna excepción de la cadena `__cause__`/`__context__` es un error TLS?"""
    while exc is not None:
        if isinstance(exc, ssl.SSLError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False
