"""TLS del Agent: Uvicorn con las opciones de `serve()` y una CA de `trustme`."""

import ssl

import httpx
import pytest

from dyndnsarr_shared.tls import build_ssl_context, has_tls_error


def get(agent, path, verify, token=True):
    headers = {"Authorization": f"Bearer {agent.token}"} if token else {}
    return httpx.get(f"{agent.url}{path}", headers=headers, verify=verify, timeout=5)


def test_a_client_with_the_ca_connects_to_the_api_and_to_healthz(tls_agent):
    context = build_ssl_context(tls_agent.ca_file)
    health = get(tls_agent, "/internal/v1/health", context)
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert get(tls_agent, "/healthz", context, token=False).status_code == 200


def test_a_client_without_the_ca_fails_the_handshake(tls_agent):
    with pytest.raises(httpx.ConnectError) as exc:
        get(tls_agent, "/internal/v1/health", build_ssl_context(None))
    assert has_tls_error(exc.value)


def test_a_client_with_another_ca_fails_the_handshake(tls_agent):
    with pytest.raises(httpx.ConnectError) as exc:
        get(tls_agent, "/internal/v1/health", build_ssl_context(tls_agent.other_ca_file))
    assert has_tls_error(exc.value)


def test_plain_http_to_the_tls_port_gets_nothing(tls_agent):
    with pytest.raises(httpx.HTTPError):
        httpx.get(
            f"http://127.0.0.1:{tls_agent.port}/internal/v1/health",
            headers={"Authorization": f"Bearer {tls_agent.token}"},
            timeout=5,
        )


def test_the_shared_context_always_verifies():
    context = build_ssl_context(None)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_serve_passes_the_tls_files_to_uvicorn_only_when_configured(
    tls_agent, monkeypatch, tmp_path
):
    from dyndnsarr_agent import cli
    from dyndnsarr_agent.config import Settings
    from dyndnsarr_agent.main import server_options

    assert "ssl_certfile" not in server_options(Settings(data_dir=tmp_path))
    options = server_options(tls_agent.settings)
    assert options["ssl_certfile"].endswith("agent-cert.pem")
    assert options["ssl_keyfile"].endswith("agent-key.pem")

    captured = {}
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(tls_agent.settings.data_dir))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(tls_agent.settings.config_file))
    monkeypatch.setenv("DYNDNSARR_AGENT_API_TOKEN", tls_agent.token)
    monkeypatch.setenv("DYNDNSARR_AGENT_TLS_CERT_FILE", options["ssl_certfile"])
    monkeypatch.setenv("DYNDNSARR_AGENT_TLS_KEY_FILE", options["ssl_keyfile"])
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    cli.serve()
    assert captured["ssl_certfile"] == options["ssl_certfile"]
    assert captured["ssl_keyfile"] == options["ssl_keyfile"]
