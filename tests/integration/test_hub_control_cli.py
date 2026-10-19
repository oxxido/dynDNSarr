"""CLI de control del Hub contra un Agent real con TLS (`TlsAgent`) y contra transportes falsos."""

import json
import re
import sqlite3
import time
import uuid

import httpx
import pytest

from dyndnsarr_hub import cli
from dyndnsarr_hub.db import MIGRATIONS, agents_repo
from dyndnsarr_shared.db import connect, migrate, transaction

AGENT = "raspberry-home"
GOOD = 'check_interval_seconds = 120\n[[records]]\nname = "new.example.com"\nmode = "monitored"\n'


@pytest.fixture
def env(tmp_path, monkeypatch, tls_agent):
    """Hub con el agente registrado y apuntando al Agent TLS de pruebas."""
    data = tmp_path / "hub-cli"
    monkeypatch.setenv("DYNDNSARR_HUB_DATA_DIR", str(data))
    monkeypatch.setenv("DYNDNSARR_HUB_AGENT_CA_FILE", str(tls_agent.ca_file))
    assert cli.run(["agent-add", AGENT]) == 0
    assert cli.run(["agent-set-api", AGENT, tls_agent.url]) == 0
    conn = connect(data / "hub.db")
    migrate(conn, MIGRATIONS)
    with transaction(conn):
        agents_repo.set_api_token(conn, AGENT, tls_agent.token)
    conn.close()
    return data


def run(capsys, *argv, transport=None):
    capsys.readouterr()
    code = cli.run(list(argv), transport=transport)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write(tmp_path, text, name="push.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def hub_rows(env, sql):
    conn = sqlite3.connect(env / "hub.db")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def wait_for_command(capsys, command_id, status="succeeded"):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, out, _ = run(capsys, "command-show", "--refresh", command_id)
        if f"-> {status}" in out:
            return out
        time.sleep(0.05)
    pytest.fail(f"el comando no llegó a {status}")


def command_id_from(out):
    return re.search(r"Comando ([0-9a-f-]{36}) creado", out).group(1)


# --- configuración ---


def test_config_push_applies_and_config_show_reports_it(env, tls_agent, tmp_path, capsys):
    code, out, err = run(capsys, "config-push", AGENT, str(write(tmp_path, GOOD)))
    assert code == 0 and "Versión 1 creada" in out and "aplicada por el Agent" in out
    assert tls_agent.app.state.holder.version == 1
    assert [r.name for r in tls_agent.app.state.holder.config.records] == ["new.example.com"]
    code, out, _ = run(capsys, "config-show", AGENT)
    assert code == 0 and "Versión deseada: 1" in out and "Versión aplicada en el Agent: 1" in out
    assert "Estado: aplicada" in out and '"check_interval_seconds": 120' in out


def test_an_invalid_config_is_rejected_and_the_agent_keeps_the_previous_one(
    env, tls_agent, tmp_path, capsys
):
    run(capsys, "config-push", AGENT, str(write(tmp_path, GOOD)))
    bad = write(tmp_path, "check_interval_seconds = 5\n", "bad.toml")
    code, out, err = run(capsys, "config-push", AGENT, str(bad))
    assert code == 1 and "rechazada por el Agent (invalid_config)" in err
    assert "check_interval_seconds" in err
    assert tls_agent.app.state.holder.version == 1
    _, out, _ = run(capsys, "config-show", AGENT)
    assert "Versión deseada: 2" in out and "Versión aplicada en el Agent: 1" in out
    assert "Último intento: versión 2 rejected (invalid_config)" in out
    assert "pendiente de aplicar" in out


def test_a_local_section_is_rejected_by_the_agent(env, tmp_path, capsys):
    text = GOOD + '[providers.dondominio]\nurl = "https://evil.example/json/"\n'
    code, _, err = run(capsys, "config-push", AGENT, str(write(tmp_path, text)))
    assert code == 1 and "forbidden_section" in err and "providers" in err


def test_with_the_agent_down_the_version_stays_pending_and_resend_delivers_it(
    env, tls_agent, tmp_path, capsys
):
    assert cli.run(["agent-set-api", AGENT, "https://127.0.0.1:1"]) == 0
    code, out, err = run(capsys, "config-push", AGENT, str(write(tmp_path, GOOD)))
    assert code == 1 and "connect_error" in err and "config-resend" in err
    assert hub_rows(env, "SELECT version FROM desired_config_versions") == [(1,)]
    _, out, _ = run(capsys, "config-show", AGENT)
    assert "pendiente de aplicar" in out and "ninguna (manda su TOML)" in out

    assert cli.run(["agent-set-api", AGENT, tls_agent.url]) == 0
    code, out, _ = run(capsys, "config-resend", AGENT)
    assert code == 0 and "aplicada" in out
    assert tls_agent.app.state.holder.version == 1
    code, out, _ = run(capsys, "config-resend", AGENT)  # idempotente
    assert code == 0 and "aplicada" in out
    assert hub_rows(env, "SELECT version FROM desired_config_versions") == [(1,)]


def test_config_resend_without_versions_and_config_show_empty(env, capsys):
    code, _, err = run(capsys, "config-resend", AGENT)
    assert code == 1 and "config-push" in err
    code, out, _ = run(capsys, "config-show", AGENT)
    assert code == 0 and "Versión deseada: ninguna" in out


def test_config_push_reads_toml_errors_cleanly(env, tmp_path, capsys):
    code, _, err = run(capsys, "config-push", AGENT, str(write(tmp_path, "esto = = no")))
    assert code == 1 and "TOML inválido" in err
    code, _, err = run(capsys, "config-push", AGENT, str(tmp_path / "no-existe.toml"))
    assert code == 1 and "No se puede leer" in err
    dated = write(tmp_path, "when = 2026-10-19\n", "dated.toml")
    code, _, err = run(capsys, "config-push", AGENT, str(dated))
    assert code == 1 and "JSON" in err
    assert hub_rows(env, "SELECT count(*) FROM desired_config_versions") == [(0,)]


# --- comandos ---


def test_command_prints_the_id_and_command_show_follows_it_to_the_end(env, capsys):
    code, out, _ = run(capsys, "command", AGENT, "check_dns")
    assert code == 0 and "-> sent" in out
    command_id = command_id_from(out)
    done = wait_for_command(capsys, command_id)
    assert "check_dns -> succeeded" in done and "records" in done
    _, out, _ = run(capsys, "command-show", command_id)  # sin --refresh: lo que el Hub sabe
    assert "-> succeeded" in out


def test_an_unsupported_command_is_marked_failed_with_the_agents_code(env, capsys):
    code, out, _ = run(capsys, "command", AGENT, "wake_device")
    assert code == 1 and "-> failed" in out and "unsupported_command" in out


def test_with_the_agent_down_the_command_stays_pending(env, tls_agent, capsys):
    cli.run(["agent-set-api", AGENT, "https://127.0.0.1:1"])
    code, out, err = run(capsys, "command", AGENT, "sync_ddns")
    assert code == 1 and "no se pudo enviar".lower() in err.lower() and "connect_error" in err
    command_id = command_id_from(out)
    assert hub_rows(env, "SELECT status FROM commands") == [("pending",)]
    cli.run(["agent-set-api", AGENT, tls_agent.url])
    code, out, err = run(capsys, "command-show", "--refresh", command_id)
    # El Agent no conoce ese id: sigue pendiente (ni se reenvía solo ni se da por fallido).
    assert code == 0 and "-> pending" in out and "no conoce ese comando" in err


def test_command_show_of_an_unknown_command(env, capsys):
    code, _, err = run(capsys, "command-show", str(uuid.uuid4()))
    assert code == 1 and "No existe el comando" in err


# --- agent-status ---


def test_agent_status_shows_the_live_state(env, tmp_path, capsys):
    run(capsys, "config-push", AGENT, str(write(tmp_path, GOOD)))
    code, out, _ = run(capsys, "agent-status", AGENT)
    assert code == 0 and "estado ok" in out
    assert "Configuración activa: versión 1" in out
    assert "new.example.com  monitored" in out


# --- errores de uso y seguridad ---


def test_missing_api_configuration_is_explained(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DYNDNSARR_HUB_DATA_DIR", str(tmp_path / "bare"))
    cli.run(["agent-add", AGENT])
    code, _, err = run(capsys, "agent-status", AGENT)
    assert code == 1 and "agent-set-api" in err
    cli.run(["agent-set-api", AGENT, "https://pi.lan:8001"])
    code, _, err = run(capsys, "agent-status", AGENT)
    assert code == 1 and "agent-api-token" in err
    code, _, err = run(capsys, "agent-status", "nadie")
    assert code == 1 and "no existe" in err


def test_agent_set_api_refuses_unsafe_urls(env, capsys):
    for url in ("http://pi.lan:8001", "https://u:p@pi.lan", "pi.lan"):
        code, _, err = run(capsys, "agent-set-api", AGENT, url)
        assert code == 1 and "URL no válida" in err
    code, _, err = run(capsys, "agent-set-api", "nadie", "https://pi.lan")
    assert code == 1 and "no existe" in err


def test_agent_api_token_is_printed_once_and_never_listed(env, capsys):
    code, out, _ = run(capsys, "agent-api-token", AGENT)
    token = next(line for line in out.splitlines() if line.startswith("ddh_"))
    assert code == 0 and len(token) >= 40 and "DYNDNSARR_AGENT_API_TOKEN" in out
    assert hub_rows(env, "SELECT api_token FROM agents") == [(token,)]
    _, out2, _ = run(capsys, "agent-api-token", AGENT)
    assert token not in out2
    _, listing, _ = run(capsys, "agent-list")
    assert token not in listing and "api: https://127.0.0.1" in listing
    code, _, err = run(capsys, "agent-api-token", "nadie")
    assert code == 1 and "no existe" in err


def test_no_token_appears_in_any_output(env, tls_agent, tmp_path, capsys):
    outputs = []
    for argv in (
        ("config-push", AGENT, str(write(tmp_path, GOOD))),
        ("config-show", AGENT),
        ("agent-status", AGENT),
        ("command", AGENT, "check_dns"),
        ("agent-list",),
        ("events",),
    ):
        outputs.append(run(capsys, *argv))
    cli.run(["agent-api-token", AGENT])  # token nuevo: el Agent responderá 401
    capsys.readouterr()
    outputs.append(run(capsys, "agent-status", AGENT))
    blob = json.dumps(outputs)
    assert tls_agent.token not in blob
    assert outputs[-1][0] == 1 and "auth_failed" in outputs[-1][2]


def test_an_untrusted_or_missing_ca_is_reported_without_sending_the_token(
    env, tls_agent, monkeypatch, capsys
):
    monkeypatch.setenv("DYNDNSARR_HUB_AGENT_CA_FILE", str(tls_agent.other_ca_file))
    code, _, err = run(capsys, "agent-status", AGENT)
    assert code == 1 and "tls_error" in err
    monkeypatch.setenv("DYNDNSARR_HUB_AGENT_CA_FILE", str(env / "no-existe.pem"))
    code, _, err = run(capsys, "agent-status", AGENT)
    assert code == 1 and "DYNDNSARR_HUB_AGENT_CA_FILE" in err


def test_a_fake_transport_is_used_when_given(env, capsys):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "agent_id": None,
                "version": "9.9.9",
                "status": "ok",
                "time_utc": "2026-10-19T12:00:00Z",
                "last_local_dns_check_at": None,
            },
        )

    # `agent-status` también pide /status: con una respuesta que no es su forma → bad_response.
    code, _, err = run(capsys, "agent-status", AGENT, transport=httpx.MockTransport(handler))
    assert code == 1 and "bad_response" in err
