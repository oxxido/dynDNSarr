from pathlib import Path

import pytest
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, component):
    import os

    for name in list(os.environ):
        if name.startswith("DYNDNSARR_"):
            monkeypatch.delenv(name)


def test_defaults_are_safe(component):
    s = component.module("config").Settings()
    assert s.host == "127.0.0.1"
    assert s.port == component.default_port
    assert s.data_dir == Path(component.default_data_dir)
    assert s.log_level == "INFO"
    assert s.db_path == Path(component.default_data_dir) / component.db_filename


def test_environment_overrides_defaults(component, monkeypatch, tmp_path):
    monkeypatch.setenv(component.env_prefix + "HOST", "0.0.0.0")
    monkeypatch.setenv(component.env_prefix + "PORT", "9999")
    monkeypatch.setenv(component.env_prefix + "DATA_DIR", str(tmp_path))
    monkeypatch.setenv(component.env_prefix + "LOG_LEVEL", "DEBUG")
    s = component.module("config").Settings()
    assert (s.host, s.port, s.data_dir, s.log_level) == ("0.0.0.0", 9999, tmp_path, "DEBUG")


@pytest.mark.parametrize(
    "name,value", [("PORT", "0"), ("PORT", "70000"), ("PORT", "abc"), ("LOG_LEVEL", "LOUD")]
)
def test_invalid_values_are_rejected(component, monkeypatch, name, value):
    monkeypatch.setenv(component.env_prefix + name, value)
    with pytest.raises(ValidationError):
        component.module("config").Settings()


def test_other_component_variables_are_ignored(component, monkeypatch):
    other = "DYNDNSARR_HUB_" if component.name == "agent" else "DYNDNSARR_AGENT_"
    monkeypatch.setenv(other + "PORT", "1234")
    assert component.module("config").Settings().port == component.default_port


def test_main_aborts_with_clear_message_on_invalid_config(component, monkeypatch, capsys):
    monkeypatch.setenv(component.env_prefix + "PORT", "not-a-port")
    with pytest.raises(SystemExit) as exc:
        component.module("__main__").main()
    assert exc.value.code not in (0, None)
    message = str(exc.value.code)
    assert component.env_prefix + "PORT" in message
    assert "not-a-port" not in message
