import pytest
from pydantic import ValidationError

from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.ddns.config import (
    ConfigError,
    DdnsConfig,
    load_ddns_config,
    require_hub_token,
    require_provider_credentials,
)

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
        (lambda t: replace(t, "monitored", "managed"), "provider"),
        (
            lambda t: replace(
                t, 'mode = "monitored"', 'mode = "monitored"\nprovider = "dondominio"'
            ),
            "monitored",
        ),
        (
            lambda t: replace(t, 'mode = "monitored"', 'mode = "managed"\nprovider = "otro"'),
            "provider",
        ),
        (
            lambda t: t + "\n[update]\nverify_retry_seconds = 60\nverify_timeout_seconds = 30\n",
            "verify_timeout",
        ),
        (lambda t: t + "\n[update]\nverify_retry_seconds = 0\n", "verify_retry_seconds"),
        (lambda t: t + '\n[providers.dondominio]\nurl = "http://example.com/json/"\n', "https"),
        (
            lambda t: t + '\n[providers.dondominio]\nurl = "https://user:pw@example.com/json/"\n',
            "credenciales",
        ),
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


MANAGED = (
    VALID.replace('mode = "monitored"', 'mode = "managed"\nprovider = "dondominio"')
    + """
[update]
verify_retry_seconds = 1
verify_timeout_seconds = 30

[providers.dondominio]
url = "http://127.0.0.1:9999/json/"
"""
)


def test_managed_record_with_update_and_provider_sections(tmp_path):
    config = load_ddns_config(write(tmp_path, MANAGED))
    [record] = config.records
    assert (record.mode, record.provider) == ("managed", "dondominio")
    assert (config.update.verify_retry_seconds, config.update.verify_timeout_seconds) == (1, 30)
    assert config.providers.dondominio.url == "http://127.0.0.1:9999/json/"


def test_update_and_provider_sections_have_defaults(tmp_path):
    config = load_ddns_config(write(tmp_path, VALID))
    assert (config.update.verify_retry_seconds, config.update.verify_timeout_seconds) == (60, 900)
    assert config.providers.dondominio.url == "https://dondns.dondominio.com/json/"
    assert config.records[0].provider is None


def managed_config(tmp_path, enabled=True):
    config = load_ddns_config(write(tmp_path, MANAGED))
    return config.model_copy(
        update={"records": [r.model_copy(update={"enabled": enabled}) for r in config.records]}
    )


@pytest.mark.parametrize(
    "user,key,missing",
    [
        (None, None, ["DONDOMINIO_USER", "DONDOMINIO_KEY"]),
        ("u", None, ["DONDOMINIO_KEY"]),
        ("", "k", ["DONDOMINIO_USER"]),
    ],
)
def test_managed_record_without_credentials_is_a_clear_error(tmp_path, user, key, missing):
    config = managed_config(tmp_path)
    with pytest.raises(ConfigError, match="credenciales") as exc:
        require_provider_credentials(config, Settings(dondominio_user=user, dondominio_key=key))
    for name in missing:
        assert f"DYNDNSARR_AGENT_{name}" in str(exc.value)


def test_credentials_are_not_needed_without_enabled_managed_records(tmp_path):
    require_provider_credentials(managed_config(tmp_path, enabled=False), Settings())
    require_provider_credentials(load_ddns_config(write(tmp_path, VALID)), Settings())
    require_provider_credentials(
        managed_config(tmp_path), Settings(dondominio_user="u", dondominio_key="k")
    )


def test_credentials_come_from_the_environment_as_secrets(monkeypatch):
    monkeypatch.setenv("DYNDNSARR_AGENT_DONDOMINIO_USER", "alice")
    monkeypatch.setenv("DYNDNSARR_AGENT_DONDOMINIO_KEY", "canary-key")
    settings = Settings()
    assert settings.dondominio_key.get_secret_value() == "canary-key"
    assert "canary-key" not in repr(settings) and "alice" not in repr(settings)


def test_invalid_environment_message_never_shows_the_key(monkeypatch):
    from dyndnsarr_agent.cli import _load_settings

    monkeypatch.setenv("DYNDNSARR_AGENT_DONDOMINIO_KEY", "canary-key")
    monkeypatch.setenv("DYNDNSARR_AGENT_PORT", "nope")
    with pytest.raises(SystemExit) as exc:
        _load_settings()
    assert "canary-key" not in str(exc.value.code)


HUB_BASE = """
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
"""


def hub_config(tmp_path, hub: str):
    return load_ddns_config(write(tmp_path, HUB_BASE + hub))


def test_hub_section_is_optional(tmp_path):
    assert hub_config(tmp_path, "").hub is None


def test_hub_section_is_parsed(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    config = hub_config(
        tmp_path,
        f'[hub]\nurl = "https://hub.example.lan/"\nagent_id = "raspberry-home"\nca_file = "{ca}"\n',
    )
    assert config.hub.url == "https://hub.example.lan"
    assert config.hub.agent_id == "raspberry-home"
    assert config.hub.ca_file == ca


def test_hub_http_is_allowed_only_to_loopback(tmp_path):
    ok = hub_config(tmp_path, '[hub]\nurl = "http://127.0.0.1:8000"\nagent_id = "a"\n')
    assert ok.hub.url == "http://127.0.0.1:8000"
    with pytest.raises(ConfigError, match="solo https"):
        hub_config(tmp_path, '[hub]\nurl = "http://hub.example.lan"\nagent_id = "a"\n')


@pytest.mark.parametrize(
    ("hub", "message"),
    [
        ('url = "https://"\nagent_id = "a"', "URL no válida"),
        ('url = "https://u:p@hub.example.lan"\nagent_id = "a"', "URL no válida"),
        ('url = "https://hub.example.lan/?x=1"\nagent_id = "a"', "parámetros"),
        ('url = "https://hub.example.lan"\nagent_id = "Mal_Id"', "agent_id"),
        (
            'url = "https://hub.example.lan"\nagent_id = "a"\nca_file = "/no/existe.pem"',
            "no existe",
        ),
        ('url = "https://hub.example.lan"\nagent_id = "a"\ntoken = "x"', "token"),
        ('agent_id = "a"', "url"),
    ],
)
def test_invalid_hub_sections_are_rejected(tmp_path, hub, message):
    with pytest.raises(ConfigError, match=message):
        hub_config(tmp_path, f"[hub]\n{hub}\n")


def test_hub_requires_the_token_from_the_environment(tmp_path):
    config = hub_config(tmp_path, '[hub]\nurl = "https://hub.example.lan"\nagent_id = "a"\n')
    with pytest.raises(ConfigError, match="DYNDNSARR_AGENT_HUB_TOKEN") as exc:
        require_hub_token(config, Settings(data_dir=tmp_path))
    assert "dda_" not in str(exc.value)
    require_hub_token(config, Settings(data_dir=tmp_path, hub_token="dda_valor"))
    with pytest.raises(ConfigError):
        require_hub_token(config, Settings(data_dir=tmp_path, hub_token=""))


def test_no_token_needed_without_hub_section(tmp_path):
    require_hub_token(hub_config(tmp_path, ""), Settings(data_dir=tmp_path))
