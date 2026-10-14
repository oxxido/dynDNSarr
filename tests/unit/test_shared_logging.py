import json
import logging

import pytest

from dyndnsarr_shared.logging import configure_logging


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = saved


def log_with_extra(capsys, **extra) -> dict:
    """Configura el logging (tras activar capsys, que sustituye sys.stderr) y emite una línea."""
    configure_logging("INFO")
    logging.getLogger("demo").info("startup", extra=extra)
    [line] = capsys.readouterr().err.splitlines()
    return json.loads(line)


def test_log_line_is_json_with_base_fields_and_extras(capsys):
    entry = log_with_extra(capsys, port=8001)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "demo"
    assert entry["message"] == "startup"
    assert entry["port"] == 8001
    assert "ts" in entry


@pytest.mark.parametrize(
    "key", ["password", "API_TOKEN", "client_secret", "Authorization", "api_key", "dondominio_Key"]
)
def test_sensitive_keys_are_redacted(capsys, key):
    entry = log_with_extra(capsys, **{key: "hunter2", "port": 1})
    assert entry[key] == "[REDACTED]"
    assert entry["port"] == 1


def test_sensitive_keys_are_redacted_in_nested_values(capsys):
    entry = log_with_extra(capsys, headers={"Authorization": "Bearer abc", "host": "x"})
    assert entry["headers"] == {"Authorization": "[REDACTED]", "host": "x"}


def test_exception_is_logged_as_text(capsys):
    configure_logging("INFO")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("demo").exception("failed")
    entry = json.loads(capsys.readouterr().err)
    assert "ValueError: boom" in entry["exc_info"]


def test_level_filters_messages(capsys):
    configure_logging("WARNING")
    logging.getLogger("demo").info("hidden")
    assert capsys.readouterr().err == ""


def test_unserializable_extra_does_not_break_logging(capsys):
    entry = log_with_extra(capsys, obj=object())
    assert entry["obj"].startswith("<object object")
