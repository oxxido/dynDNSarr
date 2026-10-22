"""Agent: emparejamiento con el Hub por código (slice 8, ADR-0013)."""

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time

import httpx
import pytest
import trustme
import uvicorn
from fastapi import FastAPI, Request
from pydantic import SecretStr

from dyndnsarr_agent import cli
from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.db import MIGRATIONS, pairing_repo
from dyndnsarr_agent.ddns.config import ConfigError
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.main import create_app
from dyndnsarr_agent.outbox.pairing import ClaimContent, Pairer, PairingOutcome, pair_until_done
from dyndnsarr_hub.api.pairing import link_app
from dyndnsarr_hub.config import Settings as HubSettings
from dyndnsarr_hub.main import create_app as create_hub
from dyndnsarr_shared.db import connect, migrate
from dyndnsarr_shared.pairing import CHALLENGE_LABEL, mac, sha256_hex

CODE = "00112233445566778899aabbccddeeff"
HUB_TOKEN = "dda_" + "h" * 43
API_TOKEN = "ddh_" + "a" * 43
IP = "83.45.12.134"
LINK = "https://127.0.0.1:8443"


class Observer:
    async def lookup(self, name):
        return [ResolverAnswer("1.1.1.1", (IP,), None)]


@pytest.fixture
def agent_tls(tmp_path):
    """Certificado de la API del Agent y su CA, como los generaría el instalador."""
    ca = trustme.CA()
    cert = ca.issue_cert("127.0.0.1")
    files = {name: tmp_path / f"{name}.pem" for name in ("ca", "cert", "key")}
    ca.cert_pem.write_to_path(files["ca"])
    cert.cert_chain_pems[0].write_to_path(files["cert"])
    cert.private_key_pem.write_to_path(files["key"])
    return files


def content(agent_tls):
    return ClaimContent(
        "raspberry-home",
        "https://127.0.0.1:8001",
        agent_tls["ca"].read_text(),
        SecretStr(HUB_TOKEN),
        SecretStr(API_TOKEN),
    )


def hub_app(tmp_path, code=CODE):
    return create_hub(
        HubSettings(data_dir=tmp_path / "hub", link_names="127.0.0.1,localhost", pairing_code=code)
    )


def hub_rows(tmp_path, sql):
    conn = sqlite3.connect(tmp_path / "hub" / "hub.db")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def agent_conn(tmp_path):
    conn = connect(tmp_path / "agent" / "agent.db")
    migrate(conn, MIGRATIONS)
    return conn


class Server:
    """Uvicorn con TLS en un hilo (para probar el TLS de verdad)."""

    def __init__(self, app, cert_file, key_file):
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            ssl_certfile=str(cert_file),
            ssl_keyfile=str(key_file),
            log_config=None,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            assert time.monotonic() < deadline, "el servidor de pruebas no arrancó"
            time.sleep(0.02)
        self.url = f"https://127.0.0.1:{self._server.servers[0].sockets[0].getsockname()[1]}"

    def close(self):
        self._server.should_exit = True
        self._thread.join(timeout=10)


# --- protocolo contra el Hub real ---


def test_pairs_with_the_real_hub_in_process(tmp_path, agent_tls, caplog):
    caplog.set_level(logging.DEBUG)
    app = hub_app(tmp_path)

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=link_app(app))
            pairer = Pairer(
                "https://127.0.0.1:8443", SecretStr(CODE), content(agent_tls), transport
            )
            conn = agent_conn(tmp_path)
            try:
                pem = await pair_until_done(conn, pairer)
                # Respuesta perdida: el mismo canje vuelve a funcionar.
                again = await pairer.pair_once()
                return pem, again, pairing_repo.hub_ca(conn), pairing_repo.last_attempt(conn)
            finally:
                conn.close()

    pem, again, stored, (error_code, attempt_at) = asyncio.run(scenario())
    assert pem == (tmp_path / "hub" / "link-ca.pem").read_text()
    assert again.hub_ca_pem == pem
    assert stored[0] == pem and error_code is None and attempt_at
    assert hub_rows(tmp_path, "SELECT agent_id, api_url, api_token FROM agents") == [
        ("raspberry-home", "https://127.0.0.1:8001", API_TOKEN)
    ]
    logs = caplog.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert all(secret not in logs for secret in (CODE, HUB_TOKEN, API_TOKEN))


def test_pairs_over_real_tls_with_the_hub_link_certificate(tmp_path, agent_tls):
    app = hub_app(tmp_path)
    tls = app.state.link_tls
    server = Server(link_app(app), tls.cert_file, tls.key_file)
    try:
        outcome = asyncio.run(Pairer(server.url, SecretStr(CODE), content(agent_tls)).pair_once())
    finally:
        server.close()
    assert outcome == PairingOutcome(hub_ca_pem=tls.ca_pem)


def test_different_code_is_detected_before_sending_anything(tmp_path, agent_tls):
    app = hub_app(tmp_path, code="f" * 32)

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=link_app(app))
            pairer = Pairer(
                "https://127.0.0.1:8443", SecretStr(CODE), content(agent_tls), transport
            )
            return await pairer.pair_once()

    assert asyncio.run(scenario()).error_code == "hub_mac_mismatch"
    assert hub_rows(tmp_path, "SELECT * FROM agents") == []
    # El Agent no llegó a enviar un canje: el Hub no contó ningún fallo de MAC.
    assert hub_rows(tmp_path, "SELECT * FROM pairing_throttle") == []


# --- un Hub falso en medio (MITM) no recibe tokens ---


class FakeHub:
    """Hub impostor con TLS real: presenta `served_ca` y firma el desafío con `code`."""

    def __init__(self, tmp_path, code, announce_other_ca=False):
        ca, other = trustme.CA(), trustme.CA()
        cert = ca.issue_cert("127.0.0.1")
        cert_file, key_file = tmp_path / "fake-cert.pem", tmp_path / "fake-key.pem"
        cert.cert_chain_pems[0].write_to_path(cert_file)
        cert.private_key_pem.write_to_path(key_file)
        announced = (other if announce_other_ca else ca).cert_pem.bytes().decode()
        self.claims = []
        app = FastAPI()

        @app.post("/pair/v1/challenge")
        async def challenge(request: Request):
            nonce = (await request.json())["nonce"]
            return {
                "ca_pem": announced,
                "mac": mac(code, CHALLENGE_LABEL, nonce, sha256_hex(announced)),
            }

        @app.post("/pair/v1/claim")
        async def claim(request: Request):
            self.claims.append(await request.body())
            return {"agent_id": "raspberry-home", "mac": "0" * 64}

        self.server = Server(app, cert_file, key_file)


def test_impostor_without_the_code_gets_no_tokens(tmp_path, agent_tls):
    fake = FakeHub(tmp_path, code="f" * 32)
    try:
        pairer = Pairer(fake.server.url, SecretStr(CODE), content(agent_tls))
        outcome = asyncio.run(pairer.pair_once())
    finally:
        fake.server.close()
    assert outcome.error_code == "hub_mac_mismatch" and outcome.hub_ca_pem is None
    assert fake.claims == []


def test_relayed_ca_does_not_match_the_impostor_certificate(tmp_path, agent_tls):
    # Aunque el MAC del desafío sea válido (reenviado del Hub real), el canje exige que el TLS lo
    # firme la CA anunciada: el impostor no tiene su clave y no recibe nada.
    fake = FakeHub(tmp_path, code=CODE, announce_other_ca=True)
    try:
        pairer = Pairer(fake.server.url, SecretStr(CODE), content(agent_tls))
        outcome = asyncio.run(pairer.pair_once())
    finally:
        fake.server.close()
    assert outcome.error_code == "tls_error"
    assert fake.claims == []


# --- reintentos ---


class ScriptedPairer:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    async def pair_once(self):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_retries_with_backoff_and_logs_only_code_changes(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    pem = trustme.CA().cert_pem.bytes().decode()
    pairer = ScriptedPairer(
        PairingOutcome(error_code="connect_error"),
        PairingOutcome(error_code="connect_error"),
        RuntimeError("boom con datos"),
        PairingOutcome(hub_ca_pem=pem),
    )
    waits, states = [], []
    conn = agent_conn(tmp_path)

    async def sleep(seconds):
        waits.append(seconds)
        states.append(pairing_repo.last_attempt(conn)[0])

    try:
        result = asyncio.run(pair_until_done(conn, pairer, sleep=sleep, jitter=lambda: 1.0))
        assert result == pem and pairing_repo.hub_ca(conn)[0] == pem
        assert pairing_repo.last_attempt(conn)[0] is None
    finally:
        conn.close()
    assert waits == [30.0, 60.0, 120.0]
    assert states == ["connect_error", "connect_error", "internal_error"]
    failed = [r.code for r in caplog.records if r.message == "pairing_failed"]
    assert failed == ["connect_error", "internal_error"]
    assert "boom con datos" not in caplog.text


# --- en el ciclo de vida del Agent ---

TOML = """
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
[hub]
url = "https://127.0.0.1:8443"
agent_id = "raspberry-home"
[[records]]
name = "home.example.com"
mode = "monitored"
"""

NO_HUB = TOML.replace(f'[hub]\nurl = "{LINK}"\nagent_id = "raspberry-home"\n', "")


def agent_settings(tmp_path, agent_tls, toml=TOML, **extra):
    config = tmp_path / "agent.toml"
    config.write_text(toml, encoding="utf-8")
    values = {
        "data_dir": tmp_path / "agent",
        "config_file": config,
        "pairing_code": CODE,
        "hub_token": HUB_TOKEN,
        "api_token": API_TOKEN,
        "tls_cert_file": agent_tls["cert"],
        "tls_key_file": agent_tls["key"],
        "tls_ca_file": agent_tls["ca"],
    }
    return Settings(**{**values, **extra})


def make_agent(settings, hub_transport):
    return create_app(
        settings,
        observer=Observer(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=IP)),
        hub_transport=hub_transport,
    )


def test_agent_pairs_and_delivers_without_manual_steps(tmp_path, agent_tls):
    hub = hub_app(tmp_path)
    agent = make_agent(agent_settings(tmp_path, agent_tls), httpx.ASGITransport(link_app(hub)))

    async def scenario():
        async with hub.router.lifespan_context(hub), agent.router.lifespan_context(agent):
            for _ in range(200):
                delivered = hub_rows(tmp_path, "SELECT type FROM events")
                if ("agent_started",) in delivered:
                    return delivered
                await asyncio.sleep(0.05)
            pytest.fail("el Agent no entregó eventos tras emparejar")

    asyncio.run(scenario())
    conn = agent_conn(tmp_path)
    try:
        assert pairing_repo.hub_ca(conn)[0] == (tmp_path / "hub" / "link-ca.pem").read_text()
    finally:
        conn.close()


def test_unreachable_hub_does_not_stop_ddns(tmp_path, agent_tls):
    def down(request):
        raise httpx.ConnectError("hub caído")

    agent = make_agent(agent_settings(tmp_path, agent_tls), httpx.MockTransport(down))

    async def scenario():
        async with agent.router.lifespan_context(agent):
            for _ in range(200):
                conn = sqlite3.connect(tmp_path / "agent" / "agent.db")
                try:
                    code = pairing_repo.last_attempt(conn)[0]
                    checked = conn.execute("SELECT count(*) FROM dns_checks").fetchone()[0]
                finally:
                    conn.close()
                if code and checked:
                    return code, agent.state.ddns_task.done(), agent.state.outbox_task.done()
                await asyncio.sleep(0.05)
            pytest.fail("no se registró el intento ni el ciclo DDNS")

    assert asyncio.run(scenario()) == ("connect_error", False, False)


@pytest.mark.parametrize(
    "toml, extra, message",
    [
        (NO_HUB, {}, "[hub]"),
        (TOML, {"tls_ca_file": None}, "DYNDNSARR_AGENT_TLS_CA_FILE"),
        (TOML, {"api_token": None}, "DYNDNSARR_AGENT_API_TOKEN"),
        (TOML, {"host": "0.0.0.0"}, "DYNDNSARR_AGENT_HOST"),
    ],
)
def test_pairing_requirements_stop_the_agent(tmp_path, agent_tls, toml, extra, message):
    with pytest.raises(ConfigError, match=message.replace("[", r"\[")):
        make_agent(agent_settings(tmp_path, agent_tls, toml=toml, **extra), None)


def test_weak_code_stops_the_agent_naming_the_variable(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "dyndnsarr_agent"],
        env={
            **os.environ,
            "DYNDNSARR_AGENT_DATA_DIR": str(tmp_path),
            "DYNDNSARR_AGENT_PAIRING_CODE": "deadbeef",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "DYNDNSARR_AGENT_PAIRING_CODE" in result.stderr and "deadbeef" not in result.stderr


# --- consola ---


def test_pair_status_and_reset(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(tmp_path / "agent"))
    monkeypatch.setenv("DYNDNSARR_AGENT_PAIRING_CODE", CODE)
    conn = agent_conn(tmp_path)
    try:
        from datetime import UTC, datetime

        pairing_repo.save_failure(conn, "hub_mac_mismatch", datetime.now(UTC))
    finally:
        conn.close()
    assert cli.run(["pair-status"]) == 1
    out = capsys.readouterr().out
    assert "hub_mac_mismatch" in out and "No se envió ningún token" in out and CODE not in out
    conn = agent_conn(tmp_path)
    try:
        from datetime import UTC, datetime

        pairing_repo.save_paired(conn, trustme.CA().cert_pem.bytes().decode(), datetime.now(UTC))
    finally:
        conn.close()
    assert cli.run(["pair-status"]) == 0
    assert "Emparejado con el Hub" in capsys.readouterr().out
    assert cli.run(["pair-reset"]) == 0
    assert "Emparejamiento olvidado" in capsys.readouterr().out
    assert cli.run(["pair-status"]) == 1
