import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_PARTS = ("password", "token", "secret", "key", "authorization")
# Atributos que `logging` pone en todo LogRecord; el resto vienen de `extra=`.
_RECORD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _is_sensitive(key: str) -> bool:
    key = key.lower()
    return any(part in key for part in _SENSITIVE_PARTS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: REDACTED if _is_sensitive(str(k)) else _redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


class RedactingFilter(logging.Filter):
    """Oculta los valores de `extra=` cuya clave parece sensible (también en dicts anidados)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in set(record.__dict__) - _RECORD_ATTRS:
            record.__dict__[key] = REDACTED if _is_sensitive(key) else _redact(record.__dict__[key])
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({k: v for k, v in record.__dict__.items() if k not in _RECORD_ATTRS})
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str) -> None:
    """Deja en el logger raíz un único handler JSON a stderr con redacción de claves sensibles."""
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
