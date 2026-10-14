import pytest
from pydantic import ValidationError

from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.ddns.config import ConfigError, DdnsConfig, load_ddns_config

VALID = """
check_interval_seconds = 120

[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]

[dns]
resolvers = ["1.1.1.1", "8.8.8.8:5353"]

[retention]
samples_days = 7
events_days = 90

[[records]]
name = "Home.Example.com"
mode = "monitored"
"""


def write(tmp_path, text):
    path = tmp_path / "agent.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_file_loads():
    from pathlib import Path

    example = Path(__file__).parents[2] / "apps/agent/config/agent.example.toml"
    config = load_ddns_config(example)
    assert config.check_interval_seconds == 300
    assert [r.name for r in config.records] == ["home.example.com"]


def test_valid_file_is_parsed_and_names_normalised(tmp_path):
    config = load_ddns_config(write(tmp_path, VALID))
    assert config.check_interval_seconds == 120
    assert config.ip_sources.urls == ["https://a.example/ip", "https://b.example/ip"]
    assert [(r.host, r.port) for r in config.dns.resolvers] == [("1.1.1.1", 53), ("8.8.8.8", 5353)]
    assert config.records[0].name == "home.example.com"
    assert config.records[0].enabled is True
    assert (config.retention.samples_days, config.retention.events_days) == (7, 90)


def test_defaults_apply_to_optional_sections(tmp_path):
    text = """
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
"""
    config = load_ddns_config(write(tmp_path, text))
    assert config.check_interval_seconds == 300
    assert (config.retention.samples_days, config.retention.events_days) == (30, 365)
    assert config.records == []


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="no existe"):
        load_ddns_config(tmp_path / "nope.toml")


def test_invalid_toml_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="TOML"):
        load_ddns_config(write(tmp_path, "esto no = = es toml"))


def replace(text, old, new):
    assert old in text
    return text.replace(old, new)


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda t: replace(t, '"https://b.example/ip"', ""), "al menos 2"),
        (lambda t: replace(t, "https://b.example/ip", "http://b.example/ip"), "https"),
        (lambda t: replace(t, "https://b.example/ip", "https://a.example/ip"), "repetid"),
        (lambda t: replace(t, "120", "30"), "check_interval_seconds"),
        (lambda t: replace(t, '"1.1.1.1", "8.8.8.8:5353"', ""), "al menos 1"),
        (lambda t: replace(t, "8.8.8.8:5353", "dns.google"), "resolver"),
        (lambda t: replace(t, "8.8.8.8:5353", "8.8.8.8:99999"), "resolver"),
        (lambda t: replace(t, "samples_days = 7", "samples_days = 0"), "samples_days"),
        (lambda t: replace(t, "Home.Example.com", "no es un host"), "name"),
        (lambda t: replace(t, "monitored", "managed"), "slice 2b"),
        (lambda t: replace(t, "monitored", "otro"), "mode"),
        (lambda t: t + '\n[[records]]\nname = "home.example.com"\nmode = "monitored"\n', "repetid"),
        (lambda t: t + "\nunknown_key = 1\n", "unknown_key"),
    ],
)
def test_invalid_config_is_rejected(tmp_path, mutate, fragment):
    with pytest.raises(ConfigError, match=fragment):
        load_ddns_config(write(tmp_path, mutate(VALID)))


def test_http_is_allowed_only_for_loopback():
    base = {"dns": {"resolvers": ["127.0.0.1:5353"]}}
    ok = DdnsConfig.model_validate(
        {**base, "ip_sources": {"urls": ["http://127.0.0.1:8080/a", "http://localhost:8081/b"]}}
    )
    assert len(ok.ip_sources.urls) == 2
    with pytest.raises(ValidationError):
        DdnsConfig.model_validate(
            {**base, "ip_sources": {"urls": ["http://127.0.0.1:8080/a", "http://10.0.0.1/b"]}}
        )


def test_settings_config_file_is_optional(monkeypatch):
    monkeypatch.delenv("DYNDNSARR_AGENT_CONFIG_FILE", raising=False)
    assert Settings().config_file is None
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", "/etc/dyndnsarr/agent.toml")
    assert str(Settings().config_file) == "/etc/dyndnsarr/agent.toml"
