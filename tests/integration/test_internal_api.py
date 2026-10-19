"""API interna del Agent (`/internal/v1`) con el ciclo de vida real y transportes falsos."""

import json
import logging
import sqlite3
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from dyndnsarr_agent import cli
from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.ddns.config import ConfigError
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.main import create_app
from dyndnsarr_shared.control import (
    AgentHealth,
    AgentStatus,
    CommandStatus,
    ConfigResult,
    config_hash,
)

IP = "1.2.3.4"
API_TOKEN = "ddh_canary-api-token-0123456789"
HUB_TOKEN = "dda_canary-hub-token-0123456789"
DD_KEY = "canary-dondominio-key-0123"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
EXPIRES = "2099-01-01T00:00:00Z"

TOML = """
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
[hub]
url = "https://hub.example.lan"
agent_id = "raspberry-home"
[[records]]
name = "home.example.com"
mode = "monitored"
"""


class Observer:
    async def lookup(self, name):
        return [ResolverAnswer("1.1.1.1", (IP,), None)]


def sources(request):
    return httpx.Response(200, text=IP)


def hub_down(request):
    raise httpx.ConnectError("hub caído")


def make_app(tmp_path, token=API_TOKEN, toml=TOML, **settings):
    config = tmp_path / "agent.toml"
    config.write_text(toml, encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        config_file=config,
        hub_token=HUB_TOKEN,
        dondominio_user="user",
        dondominio_key=DD_KEY,
        api_token=token,
        **settings,
    )
    app = create_app(
        settings,
        observer=Observer(),
        transport=httpx.MockTransport(sources),
        hub_transport=httpx.MockTransport(hub_down),
    )
    return app, settings


def query(settings, sql):
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def wait_until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    pytest.fail("no se cumplió la condición a tiempo")


@pytest.fixture
def agent(tmp_path):
    app, settings = make_app(tmp_path)
    with TestClient(app) as client:
        # El primer ciclo ya dejó sus eventos: lo que sigue no depende del ciclo.
        wait_until(lambda: len(query(settings, "SELECT 1 FROM events")) == 3)
        yield client, settings, app


def url(path):
    return f"/internal/v1{path}"


# --- autenticación ---


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/health"),
        ("GET", "/status"),
        ("PUT", "/config"),
        ("POST", "/commands"),
        ("GET", f"/commands/{uuid.uuid4()}"),
    ],
)
def test_every_endpoint_answers_the_same_401_without_a_token_or_with_a_wrong_one(
    agent, method, path
):
    client = agent[0]
    wrong = [
        {},
        {"Authorization": "Bearer ddh_otro"},
        {"Authorization": "Bearer "},
        {"Authorization": f"Basic {API_TOKEN}"},
        {"Authorization": API_TOKEN},
        {"Authorization": f"Bearer {API_TOKEN}x"},
    ]
    bodies = []
    for headers in wrong:
        response = client.request(method, url(path), headers=headers, content=b"{}")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        body = response.json()
        body.pop("timestamp"), body.pop("request_id")
        bodies.append(body)
    assert all(b == bodies[0] for b in bodies)
    assert bodies[0]["code"] == "unauthorized"


def test_authentication_comes_before_reading_the_body(agent):
    client = agent[0]
    huge = b"x" * (300 * 1024)
    assert client.put(url("/config"), content=huge).status_code == 401


def test_without_a_token_there_is_no_internal_api(tmp_path):
    app, _ = make_app(tmp_path, token=None)
    with TestClient(app) as client:
        for headers in ({}, AUTH):
            assert client.get(url("/health"), headers=headers).status_code == 404
            assert client.get(url("/status"), headers=headers).status_code == 404
        assert client.get("/healthz").status_code == 200
    assert app.state.executor is None


def test_a_token_without_a_config_file_does_not_start(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", api_token=API_TOKEN)
    with pytest.raises(ConfigError) as exc:
        create_app(settings)
    assert "CONFIG_FILE" in str(exc.value) and API_TOKEN not in str(exc.value)


def test_healthz_keeps_its_contract_with_the_api_enabled(agent):
    response = agent[0].get("/healthz")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "component", "version"}


# --- health y status ---


def test_health(agent):
    client = agent[0]
    health = AgentHealth.model_validate(client.get(url("/health"), headers=AUTH).json())
    assert (health.agent_id, health.status) == ("raspberry-home", "ok")
    assert health.last_local_dns_check_at is not None


def test_status_matches_what_the_cli_shows(agent, monkeypatch, capsys):
    client, settings, _ = agent
    status = AgentStatus.model_validate(client.get(url("/status"), headers=AUTH).json())
    assert (status.config_version, status.config_hash) == (0, None)
    assert (status.ip_status, status.public_ip) == ("confirmed", IP)
    [record] = status.records
    assert (record.name, record.mode, record.verification) == (
        "home.example.com",
        "monitored",
        "verified",
    )
    assert status.hub.pending_events >= 1 and status.hub.last_error_code is not None

    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(settings.config_file))
    assert cli.run(["status"]) == 0
    out = capsys.readouterr().out
    assert f"IP pública: {IP} (confirmada el {status.public_ip_confirmed_at})" in out
    assert f"Última comprobación: {status.last_check_at}" in out
    assert f"  home.example.com  verified  desde {record.since}" in out
    assert "Configuración: TOML local" in out


def test_no_response_nor_log_contains_a_credential(agent, caplog):
    client = agent[0]
    caplog.set_level(logging.DEBUG)
    texts = [
        client.get(url("/health"), headers=AUTH).text,
        client.get(url("/status"), headers=AUTH).text,
        client.get(url("/status"), headers={"Authorization": "Bearer mal"}).text,
        client.put(url("/config"), headers=AUTH, content=b"{}").text,
        client.post(url("/commands"), headers=AUTH, content=b"{}").text,
    ]
    blob = "".join(texts) + caplog.text
    for secret in (API_TOKEN, HUB_TOKEN, DD_KEY, "hub.example.lan"):
        assert secret not in blob


def test_get_endpoints_do_not_modify_anything(agent):
    client, settings, _ = agent
    tables = "SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM commands), "
    tables += "(SELECT count(*) FROM configuration_versions)"
    before = query(settings, tables)
    for _ in range(3):
        client.get(url("/health"), headers=AUTH)
        client.get(url("/status"), headers=AUTH)
        client.get(url(f"/commands/{uuid.uuid4()}"), headers=AUTH)
    assert query(settings, tables) == before


# --- config ---

CONTENT = {
    "check_interval_seconds": 120,
    "records": [{"name": "new.example.com", "mode": "monitored"}],
}


def put_config(client, version=1, content=CONTENT, hash=None):
    body = {"version": version, "hash": hash or config_hash(content), "content": content}
    return client.put(url("/config"), headers=AUTH, json=body)


def test_put_config_applies_and_status_shows_the_version(agent):
    client, settings, app = agent
    response = put_config(client, 3)
    assert response.status_code == 200
    assert ConfigResult.model_validate(response.json()).result == "applied"
    status = AgentStatus.model_validate(client.get(url("/status"), headers=AUTH).json())
    assert (status.config_version, status.config_hash) == (3, config_hash(CONTENT))
    assert [r.name for r in status.records] == ["new.example.com"]
    assert app.state.holder.config.check_interval_seconds == 120
    assert query(settings, "SELECT version FROM configuration_versions") == [(3,)]


def test_put_config_rejects_and_keeps_the_previous_version(agent):
    client, _, app = agent
    put_config(client, 3)
    bad = put_config(client, 4, {"check_interval_seconds": 5})
    assert bad.status_code == 200
    result = ConfigResult.model_validate(bad.json())
    assert (result.result, result.code) == ("rejected", "invalid_config")
    status = AgentStatus.model_validate(client.get(url("/status"), headers=AUTH).json())
    assert status.config_version == 3 and [r.name for r in status.records] == ["new.example.com"]
    assert app.state.holder.config.check_interval_seconds == 120


@pytest.mark.parametrize("body", [b"no es json", b"{}", b'{"version": 1}', b"[1, 2]"])
def test_put_config_with_a_malformed_body_is_400(agent, body):
    response = agent[0].put(url("/config"), headers=AUTH, content=body)
    assert response.status_code == 400 and response.json()["code"] == "invalid_request"


def test_put_config_over_256_kib_is_413_with_or_without_content_length(agent):
    client = agent[0]
    huge = json.dumps({"version": 1, "hash": "0" * 64, "content": {"x": "a" * 300_000}})
    assert client.put(url("/config"), headers=AUTH, content=huge).status_code == 413

    def chunks():
        for _ in range(40):
            yield b"x" * 10_000  # sin Content-Length: transferencia por trozos

    assert client.put(url("/config"), headers=AUTH, content=chunks()).status_code == 413


# --- comandos ---


def command(type="check_dns", command_id=None, **extra):
    return {
        "command_id": command_id or str(uuid.uuid4()),
        "type": type,
        "requested_by": "admin:1",
        "expires_at": EXPIRES,
        **extra,
    }


def test_post_command_accepts_then_get_reports_the_result(agent):
    client, settings, _ = agent
    body = command("check_dns")
    response = client.post(url("/commands"), headers=AUTH, json=body)
    assert response.status_code == 202
    assert CommandStatus.model_validate(response.json()).status == "received"
    path = url(f"/commands/{body['command_id']}")
    wait_until(lambda: client.get(path, headers=AUTH).json()["status"] == "succeeded")
    done = CommandStatus.model_validate(client.get(path, headers=AUTH).json())
    assert done.result == {"records": {"home.example.com": "verified"}}
    assert query(settings, "SELECT count(*) FROM events WHERE type = 'command_completed'") == [(1,)]


def test_a_duplicate_post_returns_200_with_the_same_state_and_runs_once(agent):
    client, settings, _ = agent
    body = command("check_dns")
    assert client.post(url("/commands"), headers=AUTH, json=body).status_code == 202
    wait_until(
        lambda: (
            client.get(url(f"/commands/{body['command_id']}"), headers=AUTH).json()["status"]
            == "succeeded"
        )
    )
    again = client.post(url("/commands"), headers=AUTH, json=body)
    assert again.status_code == 200 and again.json()["status"] == "succeeded"
    assert query(settings, "SELECT count(*) FROM events WHERE type = 'command_completed'") == [(1,)]


def test_the_same_id_with_another_type_is_409(agent):
    client = agent[0]
    body = command("check_dns")
    client.post(url("/commands"), headers=AUTH, json=body)
    other = client.post(url("/commands"), headers=AUTH, json={**body, "type": "sync_ddns"})
    assert (other.status_code, other.json()["code"]) == (409, "command_conflict")


@pytest.mark.parametrize(
    "extra,status,code",
    [
        ({"type": "wake_device", "resource_id": "nas-1"}, 422, "unsupported_command"),
        ({"parameters": {"x": 1}}, 422, "invalid_command"),
        ({"resource_id": "nas-1"}, 422, "invalid_command"),
    ],
)
def test_unsupported_commands_are_rejected(agent, extra, status, code):
    client, settings, _ = agent
    body = {**command(), **extra}
    response = client.post(url("/commands"), headers=AUTH, json=body)
    assert (response.status_code, response.json()["code"]) == (status, code)
    assert query(settings, "SELECT count(*) FROM commands") == [(0,)]


def test_a_malformed_command_is_400_and_an_unknown_id_404(agent):
    client = agent[0]
    assert client.post(url("/commands"), headers=AUTH, content=b"{}").status_code == 400
    missing = client.get(url(f"/commands/{uuid.uuid4()}"), headers=AUTH)
    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
