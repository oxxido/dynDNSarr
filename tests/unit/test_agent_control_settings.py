import pytest
from pydantic import ValidationError

from dyndnsarr_agent.config import Settings

TOKEN = "ddh_canary-api-token-0123456789"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("DYNDNSARR_"):
            monkeypatch.delenv(name)


@pytest.fixture
def tls_files(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("x")
    key.write_text("x")
    return cert, key


def test_control_api_is_off_by_default():
    s = Settings()
    assert not s.api_enabled
    assert s.tls_cert_file is None and s.tls_key_file is None


def test_an_empty_token_does_not_enable_the_api():
    assert not Settings(api_token="").api_enabled


def test_token_on_loopback_without_tls_is_allowed():
    assert Settings(api_token=TOKEN).api_enabled
    assert Settings(api_token=TOKEN, host="localhost").api_enabled


def test_token_with_tls_on_the_lan(tls_files):
    cert, key = tls_files
    s = Settings(api_token=TOKEN, host="192.168.1.50", tls_cert_file=cert, tls_key_file=key)
    assert s.api_enabled and s.tls_enabled


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.50", "raspberry.lan"])
def test_token_outside_loopback_without_tls_does_not_start(host):
    with pytest.raises(ValidationError) as exc:
        Settings(api_token=TOKEN, host=host)
    assert "TLS" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_no_token_outside_loopback_is_allowed_without_tls():
    # Sin token no hay API de control: el comportamiento de 3a (healthz en la LAN) no cambia.
    assert not Settings(host="0.0.0.0").api_enabled


@pytest.mark.parametrize("missing", ["tls_cert_file", "tls_key_file"])
def test_a_single_tls_file_fails(tls_files, missing):
    cert, key = tls_files
    files = {"tls_cert_file": cert, "tls_key_file": key}
    del files[missing]
    with pytest.raises(ValidationError) as exc:
        Settings(**files)
    assert "TLS_CERT_FILE" in str(exc.value) and "TLS_KEY_FILE" in str(exc.value)


def test_a_missing_tls_file_fails(tmp_path, tls_files):
    cert, _ = tls_files
    with pytest.raises(ValidationError) as exc:
        Settings(tls_cert_file=cert, tls_key_file=tmp_path / "no-existe.pem")
    assert "no existe" in str(exc.value)


def test_the_token_never_appears_in_repr_or_errors():
    s = Settings(api_token=TOKEN)
    assert TOKEN not in repr(s) and TOKEN not in str(s)
    with pytest.raises(ValidationError) as exc:
        Settings(api_token=TOKEN, host="0.0.0.0")
    assert TOKEN not in repr(exc.value.errors(include_input=False))


def test_cli_reports_a_cross_field_error_without_the_token(monkeypatch):
    from dyndnsarr_agent import cli

    monkeypatch.setenv("DYNDNSARR_AGENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("DYNDNSARR_AGENT_HOST", "0.0.0.0")
    with pytest.raises(SystemExit) as exc:
        cli.run(["status"])
    message = str(exc.value.code)
    assert "hace falta TLS" in message
    assert TOKEN not in message
    assert "DYNDNSARR_AGENT_:" not in message
