import logging

from dyndnsarr_shared.logging import configure_logging


def setup_logging(level: str) -> None:
    """Logging JSON del Agent. `httpx`/`httpcore` a WARNING: sus INFO incluyen la URL completa."""
    configure_logging(level)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
