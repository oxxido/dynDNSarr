"""Hub: puerto de enlace, CA propia y emparejamiento por código (slice 8, ADR-0013)."""

import asyncio
import json
import logging
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
import trustme
from cryptography import x509
from fastapi.testclient import TestClient

from dyndnsarr_hub import link_tls
from dyndnsarr_hub.api.pairing import link_app
from dyndnsarr_hub.auth.tokens import hash_token
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_hub.services.agent_client import AgentClient, build_http_client
from dyndnsarr_shared.db import connect, migrate, transaction
from dyndnsarr_shared.pairing import (
    CHALLENGE_LABEL,
    CLAIM_LABEL,
    CLAIMED_LABEL,
    mac,
    sha256_hex,
    verify_mac,
)

CODE = "00112233445566778899aabbccddeeff"
NONCE = "ab" * 32
HUB_TOKEN = "dda_" + "h" * 43
API_TOKEN = "ddh_" + "a" * 43


def settings(tmp_path, **extra):
    values = {"data_dir": tmp_path, "link_names": "127.0.0.1,localhost", "pairing_code": CODE}
    return Settings(**{**values, **extra})


def link_client(tmp_path, **extra):
    app = create_app(settings(tmp_path, **extra))
    return TestClient(link_app(app), base_url="https://127.0.0.1:8443")


def claim_body(code=CODE, **changes):
    body = {
        "agent_id": "raspberry-home",
        "api_url": "https://192.168.1.50:8001",
        "api_ca_pem": trustme.CA().cert_pem.bytes().decode(),
        "hub_token": HUB_TOKEN,
        "api_token": API_TOKEN,
    }
    body.update(changes)
    parts = [body[k] for k in ("agent_id", "api_url", "api_ca_pem", "hub_token", "api_token")]
    return {**body, "mac": mac(code, CLAIM_LABEL, *parts)}


def rows(tmp_path, sql):
    conn = sqlite3.connect(tmp_path / "hub.db")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# --- CA y certificado del puerto de enlace ---


def test_link_tls_creates_private_files_and_keeps_them(tmp_path):
    now = datetime.now(UTC)
    first = link_tls.ensure(tmp_path, ["192.168.1.10", "hub.lan"], now)
    for name in ("link-ca.pem", "link-ca.key", "link.pem", "link.key"):
        assert stat.S_IMODE(os.stat(tmp_path / name).st_mode) == 0o600
    cert = x509.load_pem_x509_certificate(first.cert_file.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["hub.lan"]
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["192.168.1.10"]
    before = first.cert_file.read_bytes()
    again = link_tls.ensure(tmp_path, ["192.168.1.10", "hub.lan"], now)
    assert again.ca_pem == first.ca_pem and first.cert_file.read_bytes() == before


def test_link_tls_renews_server_cert_with_the_same_ca(tmp_path):
    now = datetime.now(UTC)
    first = link_tls.ensure(tmp_path, ["hub.lan"], now)
    before = first.cert_file.read_bytes()
    # Quedan 29 días: se renueva; la CA (fijada por los Agents) no cambia.
    later = link_tls.ensure(tmp_path, ["hub.lan"], now + timedelta(days=336))
    renewed = later.cert_file.read_bytes()
    assert later.ca_pem == first.ca_pem and renewed != before
    renamed = link_tls.ensure(tmp_path, ["hub2.lan"], now + timedelta(days=336))
    assert renamed.ca_pem == first.ca_pem and renamed.cert_file.read_bytes() != renewed


# --- configuración ---


@pytest.mark.parametrize(
    "env, variable",
    [
        (
            {"DYNDNSARR_HUB_PAIRING_CODE": "abc123", "DYNDNSARR_HUB_LINK_NAMES": "hub.lan"},
            "PAIRING_CODE",
        ),
        ({"DYNDNSARR_HUB_PAIRING_CODE": CODE}, "LINK_NAMES"),
        ({"DYNDNSARR_HUB_LINK_NAMES": "no valid!"}, "LINK_NAMES"),
    ],
)
def test_invalid_pairing_settings_stop_the_hub(tmp_path, env, variable):
    result = subprocess.run(
        [sys.executable, "-m", "dyndnsarr_hub"],
        env={**os.environ, "DYNDNSARR_HUB_DATA_DIR": str(tmp_path), **env},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert f"DYNDNSARR_HUB_{variable}" in result.stderr
    assert "abc123" not in result.stderr


# --- desafío ---


def test_challenge_proves_the_code_over_the_link_ca(tmp_path):
    with link_client(tmp_path) as client:
        response = client.post("/pair/v1/challenge", json={"nonce": NONCE})
    assert response.status_code == 200
    body = response.json()
    assert body["ca_pem"] == (tmp_path / "link-ca.pem").read_text()
    assert verify_mac(CODE, body["mac"], CHALLENGE_LABEL, NONCE, sha256_hex(body["ca_pem"]))
    assert not verify_mac("f" * 32, body["mac"], CHALLENGE_LABEL, NONCE, sha256_hex(body["ca_pem"]))


def test_pairing_only_on_the_link_port(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as http_port:
        assert http_port.post("/pair/v1/challenge", json={"nonce": NONCE}).status_code == 404
    with TestClient(link_app(app)) as link:
        assert link.get("/admin/login").status_code == 404
        assert link.get("/healthz").status_code == 404
        assert link.get("/").status_code == 404


def test_without_code_there_is_no_pairing(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, link_names="127.0.0.1"))
    with TestClient(link_app(app)) as client:
        assert client.post("/pair/v1/challenge", json={"nonce": NONCE}).status_code == 404


# --- canje ---


def test_claim_registers_agent_with_its_tokens_and_ca(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    body = claim_body()
    with link_client(tmp_path) as client:
        response = client.post("/pair/v1/claim", json=body)
        assert response.status_code == 200
        reply = response.json()
        assert reply["agent_id"] == "raspberry-home"
        assert verify_mac(CODE, reply["mac"], CLAIMED_LABEL, "raspberry-home", body["mac"])
        # El token que generó el Agent ya vale para entregar eventos por el puerto de enlace.
        events = client.post(
            "/api/v1/agents/raspberry-home/events",
            json={"events": []},
            headers={"Authorization": f"Bearer {HUB_TOKEN}"},
        )
        assert events.status_code == 200
    (agent,) = rows(tmp_path, "SELECT agent_id, api_url, api_token, api_ca_pem FROM agents")
    assert agent == ("raspberry-home", body["api_url"], API_TOKEN, body["api_ca_pem"])
    assert rows(tmp_path, "SELECT token_hash FROM agent_credentials") == [(hash_token(HUB_TOKEN),)]
    logs = caplog.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    for secret in (CODE, HUB_TOKEN, API_TOKEN):
        assert secret not in logs


def test_repeated_claim_is_idempotent_and_other_content_gets_code_consumed(tmp_path):
    body = claim_body()
    with link_client(tmp_path) as client:
        first = client.post("/pair/v1/claim", json=body)
        again = client.post("/pair/v1/claim", json=body)
        other = client.post(
            "/pair/v1/claim", json=claim_body(agent_id="other-pi", hub_token="dda_" + "x" * 43)
        )
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert other.status_code == 409 and other.json()["code"] == "code_consumed"
    assert rows(tmp_path, "SELECT agent_id FROM agents") == [("raspberry-home",)]
    assert len(rows(tmp_path, "SELECT * FROM pairing")) == 1


def test_consumed_code_stays_consumed_after_restart(tmp_path, caplog):
    with link_client(tmp_path) as client:
        assert client.post("/pair/v1/claim", json=claim_body()).status_code == 200
    caplog.set_level(logging.INFO)
    with link_client(tmp_path) as client:
        other = client.post("/pair/v1/claim", json=claim_body(agent_id="other-pi"))
    assert other.json()["code"] == "code_consumed"
    assert any(r.message == "pairing_code_consumed" for r in caplog.records)


def test_existing_agent_is_not_replaced(tmp_path):
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    with transaction(conn):
        agents_repo.create_agent(conn, "raspberry-home", "manual-hash", datetime.now(UTC))
    conn.close()
    with link_client(tmp_path) as client:
        response = client.post("/pair/v1/claim", json=claim_body())
    assert response.status_code == 409 and response.json()["code"] == "agent_exists"
    assert rows(tmp_path, "SELECT token_hash FROM agent_credentials") == [("manual-hash",)]
    assert rows(tmp_path, "SELECT * FROM pairing") == []


def test_bad_mac_is_counted_and_blocks_pairing(tmp_path):
    with link_client(tmp_path) as client:
        for _ in range(9):
            response = client.post("/pair/v1/claim", json=claim_body(code="f" * 32))
            assert response.status_code == 401 and response.json()["code"] == "bad_mac"
        assert client.post("/pair/v1/claim", json=claim_body(code="f" * 32)).status_code == 401
        blocked = client.post("/pair/v1/claim", json=claim_body())
        assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
        assert client.post("/pair/v1/challenge", json={"nonce": NONCE}).status_code == 429
    assert rows(tmp_path, "SELECT * FROM agents") == []


def test_claim_rejects_malformed_bodies(tmp_path):
    with link_client(tmp_path) as client:
        assert client.post("/pair/v1/claim", json={"agent_id": "x"}).status_code == 400
        too_big = client.post("/pair/v1/claim", content=b"x" * (33 * 1024))
        assert too_big.status_code == 413
        insecure = client.post("/pair/v1/claim", json=claim_body(api_url="http://192.168.1.5"))
        assert insecure.status_code == 400


# --- CA fijada por agente ---


def test_pinned_ca_is_used_only_for_its_agent(tls_agent):
    pem = tls_agent.ca_file.read_text()
    other = tls_agent.other_ca_file.read_text()

    async def health(pinned):
        async with build_http_client(None, None, pinned) as http:
            return await AgentClient(http, tls_agent.url, tls_agent.token).health()

    assert asyncio.run(health({tls_agent.url: pem})).ok
    assert asyncio.run(health({tls_agent.url: other})).error_code == "tls_error"
    # La CA fijada de otro Agent no sirve aquí: se usa la verificación general.
    assert asyncio.run(health({"https://127.0.0.1:1": pem})).error_code == "tls_error"


def test_paired_agent_appears_in_agent_list(tmp_path):
    with link_client(tmp_path) as client:
        assert client.post("/pair/v1/claim", json=claim_body()).status_code == 200
    result = subprocess.run(
        [sys.executable, "-m", "dyndnsarr_hub", "agent-list"],
        env={**os.environ, "DYNDNSARR_HUB_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("raspberry-home  activo  tokens vigentes: 1")
    assert "api: https://192.168.1.50:8001" in result.stdout
    assert API_TOKEN not in result.stdout
