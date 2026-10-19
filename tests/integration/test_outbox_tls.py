import asyncio
import logging
import ssl
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_agent.logging_setup import setup_logging
from dyndnsarr_agent.outbox import loop
from dyndnsarr_agent.outbox.client import HttpHubClient
from dyndnsarr_shared.db import connect, migrate
from dyndnsarr_shared.tls import build_ssl_context

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def deliver(tmp_path, hub, ca_file, token=None):
    """Entrega los pendientes de un Agent nuevo contra el Hub TLS. Devuelve (código, conn)."""
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    if not ddns_repo.count_pending(conn):
        ddns_repo.insert_event(conn, "agent_started", NOW, None, {"version": "x"})

    async def go():
        async with httpx.AsyncClient(verify=build_ssl_context(ca_file)) as http:
            client = HttpHubClient(http, hub.url, hub.agent_id, SecretStr(token or hub.token))
            return await loop.deliver_pending(conn, client, lambda: NOW)

    return asyncio.run(go()), conn


def test_delivers_over_tls_with_the_right_ca(tmp_path, tls_hub):
    code, conn = deliver(tmp_path, tls_hub, tls_hub.ca_file)
    assert code is None
    assert ddns_repo.count_pending(conn) == 0
    assert len(tls_hub.stored_event_ids()) == 1


def test_unknown_ca_fails_with_tls_error_and_events_stay_pending(tmp_path, tls_hub):
    # Sin ca_file se usa el almacén del sistema: no conoce la CA de pruebas.
    code, conn = deliver(tmp_path, tls_hub, None)
    assert code == "tls_error"
    assert ddns_repo.count_pending(conn) == 1
    assert tls_hub.stored_event_ids() == []
    assert ddns_repo.get_state(conn, "outbox_last_error_code") == "tls_error"


def test_a_different_ca_also_fails_and_the_token_never_reaches_the_server(tmp_path, tls_hub):
    code, conn = deliver(tmp_path, tls_hub, tls_hub.other_ca_file)
    assert code == "tls_error"
    assert ddns_repo.count_pending(conn) == 1
    assert tls_hub.stored_event_ids() == []


def test_ca_file_replaces_the_system_store(tls_hub):
    context = build_ssl_context(tls_hub.ca_file)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert len(context.get_ca_certs()) == 1  # solo la CA indicada
    default = build_ssl_context(None)
    assert default.verify_mode == ssl.CERT_REQUIRED and default.check_hostname


@pytest.fixture
def agent_logging(capsys):
    """El logging real del Agent (`setup_logging`): httpx/httpcore en WARNING, JSON a stderr."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    setup_logging("DEBUG")
    yield capsys
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def test_canary_token_and_url_are_not_in_logs_state_or_events(tmp_path, tls_hub, agent_logging):
    deliver(tmp_path, tls_hub, tls_hub.other_ca_file)  # fallo TLS
    code, conn = deliver(tmp_path, tls_hub, tls_hub.ca_file)  # éxito
    assert code is None
    dump = "\n".join(
        str(row)
        for table in ("app_state", "events")
        for row in conn.execute(f"SELECT * FROM {table}")
    )
    logs = agent_logging.readouterr().err
    assert "events_received" in logs  # la captura funciona
    for secret in (tls_hub.token, tls_hub.url):
        assert secret not in dump
        assert secret not in logs


@pytest.mark.parametrize("token", ["dda_token-equivocado", ""])
def test_a_bad_token_over_tls_is_auth_failed(tmp_path, tls_hub, token):
    code, conn = deliver(tmp_path, tls_hub, tls_hub.ca_file, token=token or "x")
    assert code == "auth_failed"
    assert ddns_repo.count_pending(conn) == 1
