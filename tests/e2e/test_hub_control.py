"""Hub y Agent como procesos reales; el Agent con TLS de una CA de `trustme` (slice 3b).

Cubre los criterios 2-8 del slice. El ciclo del daemon corre cada segundo porque un registro
`managed` queda `pending_verification` (el DNS falso nunca refleja la actualización).
"""

import json
import os
import re
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import httpx
import pytest
import trustme

from dyndnsarr_shared.tls import build_ssl_context

pytestmark = pytest.mark.e2e

A = "1.2.3.4"
AGENT_ID = "raspberry-home"
DD_USER, DD_KEY = "canary-dd-user", "canary-dd-key-0123456789"
EXPIRES = "2099-01-01T00:00:00Z"

LOCAL_TOML = """
[update]
verify_retry_seconds = 1
verify_timeout_seconds = 900
"""
GOOD = """
[[records]]
name = "old.example.com"
mode = "monitored"
[[records]]
name = "new.example.com"
mode = "monitored"
[[records]]
name = "m.example.com"
mode = "managed"
provider = "dondominio"
"""
BAD = "check_interval_seconds = 5\n"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=15)


def rows(db, sql):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def wait_until(check, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
        except (sqlite3.OperationalError, httpx.HTTPError):
            value = None
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("no se cumplió la condición a tiempo")


def wait_for_health(proc, url, ca_file=None, timeout=20):
    context = ssl.create_default_context(cafile=str(ca_file)) if ca_file else None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"el proceso terminó con código {proc.returncode}")
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=1, context=context):
                return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    proc.kill()
    pytest.fail("healthz no respondió a tiempo")


def run(*args, env, check=True):
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
    if check:
        assert result.returncode == 0, result.stderr
    return result


class Stack:
    """Un Hub (HTTP en loopback) y un Agent (HTTPS) con su CA."""

    def __init__(self, tmp_path, make_fake_ip_source, fake_dns, fake_dondominio):
        self.tmp = tmp_path
        ca = trustme.CA()
        cert = ca.issue_cert("127.0.0.1", "localhost")
        self.ca_file, self.other_ca = tmp_path / "ca.pem", tmp_path / "other-ca.pem"
        self.cert_file, self.key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
        ca.cert_pem.write_to_path(self.ca_file)
        trustme.CA().cert_pem.write_to_path(self.other_ca)
        cert.cert_chain_pems[0].write_to_path(self.cert_file)
        cert.private_key_pem.write_to_path(self.key_file)

        self.fake_dns, self.fake_dd = fake_dns, fake_dondominio
        # `m` apunta a otra IP y el DNS falso nunca la cambia: queda en verificación al actualizar.
        fake_dns.answers.update(
            {"old.example.com": [A], "new.example.com": [A], "m.example.com": ["9.9.9.9"]}
        )
        self.sources = [make_fake_ip_source(A), make_fake_ip_source(A)]
        self.hub_port, self.agent_port = free_port(), free_port()
        self.hub_data, self.agent_data = tmp_path / "hub-data", tmp_path / "agent-data"
        self.hub_db, self.agent_db = self.hub_data / "hub.db", self.agent_data / "agent.db"
        self.hub_log, self.agent_log = tmp_path / "hub.log", tmp_path / "agent.log"
        self.config = tmp_path / "agent.toml"
        self.agent_proc = self.hub_proc = None
        self.hub_token = self.api_token = None
        self.write_agent_toml()

    def write_agent_toml(self):
        self.config.write_text(
            LOCAL_TOML
            + f'[ip_sources]\nurls = ["{self.sources[0].url}", "{self.sources[1].url}"]\n'
            f'[dns]\nresolvers = ["{self.fake_dns.resolver}"]\n'
            f'[providers.dondominio]\nurl = "{self.fake_dd.url}"\n'
            f'[hub]\nurl = "http://127.0.0.1:{self.hub_port}"\nagent_id = "{AGENT_ID}"\n'
            '[[records]]\nname = "old.example.com"\nmode = "monitored"\n'
            '[[records]]\nname = "m.example.com"\nmode = "managed"\nprovider = "dondominio"\n',
            encoding="utf-8",
        )

    # --- Hub ---

    def hub_env(self, **overrides):
        return {
            **os.environ,
            "DYNDNSARR_HUB_HOST": "127.0.0.1",
            "DYNDNSARR_HUB_PORT": str(self.hub_port),
            "DYNDNSARR_HUB_DATA_DIR": str(self.hub_data),
            "DYNDNSARR_HUB_AGENT_CA_FILE": str(self.ca_file),
            # Slice 4: el refresco del estado público, acortado para no esperar un minuto.
            "DYNDNSARR_HUB_STATUS_REFRESH_SECONDS": "15",
            **overrides,
        }

    def hub(self, *args, check=True):
        return run(sys.executable, "-m", "dyndnsarr_hub", *args, env=self.hub_env(), check=check)

    def start_hub(self, **overrides):
        self.hub_proc = subprocess.Popen(
            [sys.executable, "-m", "dyndnsarr_hub"],
            env=self.hub_env(**overrides),
            stdout=subprocess.DEVNULL,
            stderr=self.hub_log.open("a"),
        )
        wait_for_health(self.hub_proc, f"http://127.0.0.1:{self.hub_port}")

    def stop_hub(self):
        stop(self.hub_proc)

    # --- Agent ---

    def agent_env(self, **overrides):
        env = {
            **os.environ,
            "DYNDNSARR_AGENT_HOST": "127.0.0.1",
            "DYNDNSARR_AGENT_PORT": str(self.agent_port),
            "DYNDNSARR_AGENT_DATA_DIR": str(self.agent_data),
            "DYNDNSARR_AGENT_CONFIG_FILE": str(self.config),
            "DYNDNSARR_AGENT_HUB_TOKEN": self.hub_token,
            "DYNDNSARR_AGENT_DONDOMINIO_USER": DD_USER,
            "DYNDNSARR_AGENT_DONDOMINIO_KEY": DD_KEY,
            "DYNDNSARR_AGENT_API_TOKEN": self.api_token,
            "DYNDNSARR_AGENT_TLS_CERT_FILE": str(self.cert_file),
            "DYNDNSARR_AGENT_TLS_KEY_FILE": str(self.key_file),
        }
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    def start_agent(self):
        self.agent_proc = subprocess.Popen(
            [sys.executable, "-m", "dyndnsarr_agent"],
            env=self.agent_env(),
            stdout=subprocess.DEVNULL,
            stderr=self.agent_log.open("a"),
        )
        wait_for_health(self.agent_proc, self.agent_url, self.ca_file)

    def stop_agent(self):
        stop(self.agent_proc)

    def agent_cli(self, *args, check=True):
        return run(
            sys.executable, "-m", "dyndnsarr_agent", *args, env=self.agent_env(), check=check
        )

    @property
    def agent_url(self):
        return f"https://127.0.0.1:{self.agent_port}"

    def api(self, method, path, token=True, ca=None, **kwargs):
        headers = {"Authorization": f"Bearer {self.api_token}"} if token is True else {}
        if isinstance(token, str):
            headers = {"Authorization": f"Bearer {token}"}
        return httpx.request(
            method,
            f"{self.agent_url}/internal/v1{path}",
            headers=headers,
            verify=build_ssl_context(ca or self.ca_file),
            timeout=10,
            **kwargs,
        )

    # --- consultas ---

    def agent_rows(self, sql):
        return rows(self.agent_db, sql)

    def hub_rows(self, sql):
        return rows(self.hub_db, sql)

    def provision(self):
        out = self.hub("agent-add", AGENT_ID).stdout
        self.hub_token = next(ln for ln in out.splitlines() if ln.startswith("dda_"))
        self.hub("agent-set-api", AGENT_ID, self.agent_url)
        out = self.hub("agent-api-token", AGENT_ID).stdout
        self.api_token = next(ln for ln in out.splitlines() if ln.startswith("ddh_"))

    def push(self, text, name="push.toml", check=True):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return self.hub("config-push", AGENT_ID, str(path), check=check)


@pytest.fixture
def stack(tmp_path, make_fake_ip_source, fake_dns, fake_dondominio):
    fake_dondominio.propagate = False  # el DNS nunca refleja: el registro queda en verificación
    s = Stack(tmp_path, make_fake_ip_source, fake_dns, fake_dondominio)
    s.provision()
    yield s
    for proc in (s.agent_proc, s.hub_proc):
        if proc is not None and proc.poll() is None:
            proc.kill()


def started(stack):
    stack.start_hub()
    stack.start_agent()
    wait_until(lambda: stack.agent_rows("SELECT 1 FROM dns_checks"))


def checks_of(stack, name):
    return stack.agent_rows(f"SELECT count(*) FROM dns_checks WHERE record_name = '{name}'")[0][0]


def test_config_flow_invalid_keeps_the_agent_and_valid_changes_the_next_cycle(stack):
    started(stack)
    status = stack.hub("agent-status", AGENT_ID).stdout
    assert "estado ok" in status and "Configuración activa: TOML local" in status

    # Inválida: el Agent la rechaza, conserva versión y registros, y el Hub la ve rechazada.
    bad = stack.push(BAD, check=False)
    assert bad.returncode == 1 and "rechazada por el Agent (invalid_config)" in bad.stderr
    assert "Configuración: TOML local" in stack.agent_cli("status").stdout
    before = checks_of(stack, "old.example.com")
    wait_until(lambda: checks_of(stack, "old.example.com") > before)  # sigue ciclando igual
    assert checks_of(stack, "new.example.com") == 0
    assert "rejected (invalid_config)" in stack.hub("config-show", AGENT_ID).stdout

    # Local: una sección que no administra el Hub se rechaza.
    local = stack.push(
        GOOD + '[providers.dondominio]\nurl = "https://evil.example/"\n', check=False
    )
    assert local.returncode == 1 and "forbidden_section" in local.stderr

    # Válida: aplicada, visible en `status`, y el siguiente ciclo usa los registros nuevos.
    good = stack.push(GOOD)
    assert "aplicada por el Agent" in good.stdout
    assert "Configuración: versión 3" in stack.agent_cli("status").stdout
    wait_until(lambda: checks_of(stack, "new.example.com") > 0)
    assert "Versión aplicada en el Agent: 3" in stack.hub("config-show", AGENT_ID).stdout

    # Reenviar la misma versión: aplicada, sin fila nueva. Versión menor o conflicto: rechazadas.
    assert "aplicada" in stack.hub("config-resend", AGENT_ID).stdout
    assert stack.agent_rows("SELECT version FROM configuration_versions") == [(3,)]
    content = json.loads(stack.agent_rows("SELECT content FROM configuration_versions")[0][0])
    from dyndnsarr_shared.control import config_hash

    stale = stack.api(
        "PUT", "/config", json={"version": 1, "hash": config_hash(content), "content": content}
    )
    assert stale.json()["code"] == "stale_version"
    conflict = stack.api(
        "PUT", "/config", json={"version": 3, "hash": "0" * 64, "content": content}
    )
    assert conflict.json()["code"] == "hash_mismatch"
    other = {"check_interval_seconds": 600}
    conflict = stack.api(
        "PUT", "/config", json={"version": 3, "hash": config_hash(other), "content": other}
    )
    assert conflict.json()["code"] == "version_conflict"

    # El Hub se entera también por los eventos (el outbox entrega cada 30 s).
    def hub_events():
        return stack.hub("events", "--agent", AGENT_ID).stdout

    wait_until(lambda: "config_rejected" in hub_events() and "config_applied" in hub_events(), 60)


def test_commands_and_link_security(stack):
    started(stack)
    body = {
        "command_id": str(uuid.uuid4()),
        "type": "check_dns",
        "requested_by": "admin:test",
        "expires_at": EXPIRES,
    }
    first = stack.api("POST", "/commands", json=body)
    assert first.status_code == 202
    path = f"/commands/{body['command_id']}"
    wait_until(lambda: stack.api("GET", path).json()["status"] == "succeeded")
    again = stack.api("POST", "/commands", json=body)
    assert again.status_code == 200 and again.json()["status"] == "succeeded"
    assert stack.agent_rows("SELECT count(*) FROM events WHERE type = 'command_completed'") == [
        (1,)
    ]  # una sola ejecución
    assert stack.api("POST", "/commands", json={**body, "type": "sync_ddns"}).status_code == 409

    # Caducado y no soportado: no se ejecutan.
    expired = stack.api(
        "POST",
        "/commands",
        json={**body, "command_id": str(uuid.uuid4()), "expires_at": "2020-01-01T00:00:00Z"},
    )
    assert expired.json()["status"] == "expired"
    wake = stack.api(
        "POST",
        "/commands",
        json={**body, "command_id": str(uuid.uuid4()), "type": "wake_device", "resource_id": "nas"},
    )
    assert wake.status_code == 422 and wake.json()["code"] == "device_not_allowed"

    # Por la consola del Hub: el comando llega a su estado final por el enlace.
    out = stack.hub("command", AGENT_ID, "check_dns").stdout
    command_id = out.split("Comando ")[1].split()[0]
    wait_until(lambda: "-> succeeded" in stack.hub("command-show", "--refresh", command_id).stdout)

    # Seguridad del enlace: sin token, token malo, CA no confiable, sin TLS.
    assert stack.api("GET", "/health", token=False).status_code == 401
    assert stack.api("GET", "/health", token="ddh_malo").status_code == 401
    with pytest.raises(httpx.ConnectError):
        stack.api("GET", "/health", ca=stack.other_ca)
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{stack.agent_port}/internal/v1/health", timeout=5)
    # Ningún GET modifica nada (el Agent parado ya no escribe: se comparan las tablas de control).
    tables = "SELECT (SELECT count(*) FROM commands), (SELECT count(*) FROM configuration_versions)"
    before = stack.agent_rows(tables)
    for _ in range(3):
        stack.api("GET", "/health")
        stack.api("GET", "/status")
        stack.api("GET", path)
    assert stack.agent_rows(tables) == before


def test_a_command_left_running_by_a_crash_is_interrupted_and_never_rerun(stack):
    started(stack)
    stack.stop_agent()
    command_id = str(uuid.uuid4())
    conn = sqlite3.connect(stack.agent_db)
    conn.execute(
        "INSERT INTO commands (command_id, type, requested_by, received_at, expires_at, status) "
        "VALUES (?, 'sync_ddns', 'admin:test', '2026-10-19T12:00:00Z', ?, 'running')",
        (command_id, EXPIRES),
    )
    conn.commit()
    conn.close()
    calls_before = len(stack.fake_dd.calls)
    stack.start_agent()
    done = stack.api("GET", f"/commands/{command_id}").json()
    assert (done["status"], done["result"]) == ("failed", {"code": "interrupted"})
    wait_until(lambda: "interrupted" in stack.hub("events", "--agent", AGENT_ID).stdout, 60)
    # No se reejecutó: el pase manual habría llamado a DonDominio por su cuenta.
    assert (
        stack.api(
            "POST",
            "/commands",
            json={
                "command_id": command_id,
                "type": "sync_ddns",
                "requested_by": "x",
                "expires_at": EXPIRES,
            },
        ).json()["status"]
        == "failed"
    )
    assert len(stack.fake_dd.calls) == calls_before


def test_refuses_to_start_without_tls_on_the_lan_and_has_no_api_without_a_token(stack):
    result = subprocess.run(
        [sys.executable, "-m", "dyndnsarr_agent"],
        env=stack.agent_env(
            DYNDNSARR_AGENT_HOST="0.0.0.0",
            DYNDNSARR_AGENT_TLS_CERT_FILE=None,
            DYNDNSARR_AGENT_TLS_KEY_FILE=None,
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0 and "hace falta TLS" in str(result.stderr + result.stdout)
    assert stack.api_token not in result.stderr + result.stdout
    # Sin token no hay API de control (y sin TLS, `healthz` en claro como en 3a).
    env = stack.agent_env(
        DYNDNSARR_AGENT_API_TOKEN=None,
        DYNDNSARR_AGENT_TLS_CERT_FILE=None,
        DYNDNSARR_AGENT_TLS_KEY_FILE=None,
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "dyndnsarr_agent"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_health(proc, f"http://127.0.0.1:{stack.agent_port}")
        for headers in ({}, {"Authorization": f"Bearer {stack.api_token}"}):
            response = httpx.get(
                f"http://127.0.0.1:{stack.agent_port}/internal/v1/health", headers=headers
            )
            assert response.status_code == 404
    finally:
        stop(proc)


def test_the_agent_works_without_the_hub_reset_returns_to_the_toml_and_nothing_leaks(stack):
    started(stack)
    assert "aplicada" in stack.push(GOOD).stdout
    stack.stop_hub()  # el Hub cae: el Agent sigue
    stack.stop_agent()
    out = stack.agent_cli("config-reset").stdout
    assert "versión 1" in out and "Reinicia" in out
    stack.start_agent()  # con el Hub caído, arranca con el TOML
    assert "Configuración: TOML local" in stack.agent_cli("status").stdout
    wait_until(
        lambda: stack.agent_rows("SELECT 1 FROM dns_checks WHERE record_name = 'm.example.com'")
    )
    assert stack.agent_rows("SELECT count(*) FROM configuration_versions") == [(0,)]

    stack.start_hub()  # al volver, el evento `config_reset` llega
    wait_until(lambda: "config_reset" in stack.hub("events", "--agent", AGENT_ID).stdout, 90)
    shown = stack.hub("config-show", AGENT_ID).stdout
    assert "ninguna (manda su TOML)" in shown and "pendiente de aplicar" in shown
    assert "aplicada" in stack.hub("config-resend", AGENT_ID).stdout
    assert "Configuración: versión 1" in stack.agent_cli("status").stdout

    # Ni el token de la API, ni la clave de DonDominio ni su URL aparecen en logs ni consolas.
    stack.stop_agent()
    stack.stop_hub()
    outputs = [
        stack.agent_cli("status").stdout,
        stack.hub("agent-list").stdout,
        stack.hub("config-show", AGENT_ID).stdout,
        stack.hub("events").stdout,
        stack.agent_log.read_text(),
        stack.hub_log.read_text(),
    ]
    blob = "\n".join(outputs)
    for secret in (stack.api_token, stack.hub_token, DD_KEY, DD_USER, stack.fake_dd.url):
        assert secret not in blob
    assert "Traceback" not in blob


def test_public_landing_shows_real_state_without_private_data_and_goes_unknown(stack):
    """Slice 4, criterios 2-7: estado real, sin la IP completa ni secretos, `Desconocido` al
    apagar el Agent y 404 con la página desactivada. La IP del Agent (`1.2.3.4`) es el canario."""
    started(stack)
    base = f"http://127.0.0.1:{stack.hub_port}"

    def status():
        body = httpx.get(f"{base}/public/v1/status", timeout=5).json()
        # Con el Agent sano el registro `managed` queda pendiente de verificación (degradado);
        # hay que esperar al refresco (15 s) y a que el outbox entregue la renovación (30 s).
        return body if body["state"] == "degraded" and body["renewals"] else None

    body = wait_until(status, 90)
    assert body["last_successful_check_at"] is not None
    assert [(r["ip_suffix"], r["verification"], r["record"]) for r in body["renewals"]] == [
        ("4", "pending", None)
    ]
    page = httpx.get(f"{base}/", timeout=5)
    assert page.status_code == 200 and "Degradado" in page.text
    assert "Pendiente de verificación" in page.text and "IP terminada en 4" in page.text
    assert "simulad" not in page.text.lower() and 'id="demoState"' not in page.text
    nonce = re.search(r"script-src 'nonce-([^']+)'", page.headers["content-security-policy"])[1]
    assert page.text.count(f'nonce="{nonce}"') == 3 and "unsafe-inline" not in str(page.headers)

    public = json.dumps(
        stack.hub_rows("SELECT * FROM public_renewals")
        + stack.hub_rows("SELECT * FROM public_agent_status")
        + stack.hub_rows("SELECT * FROM public_status_samples")
    )
    served = page.text + str(dict(page.headers)) + json.dumps(body)
    for secret in (A, "1.2.3", stack.hub_token, stack.api_token, DD_USER, DD_KEY):
        assert secret not in served + public, secret
    # SHOW_DOMAINS apagado por defecto: las tablas guardan el nombre, pero no se sirve.
    for name in ("old.example.com", "m.example.com"):
        assert name not in served

    # Agent apagado: pasada la caducidad (3 refrescos) la página pasa a Desconocido sola.
    stack.stop_agent()
    stopped = wait_until(
        lambda: (
            (b := httpx.get(f"{base}/public/v1/status", timeout=5).json())["state"] == "unknown"
            and b
        ),
        120,
    )
    assert stopped["last_successful_check_at"] == body["last_successful_check_at"]
    assert stopped["history"]["coverage"] > 0
    page = httpx.get(f"{base}/", timeout=5)
    assert "Desconocido" in page.text and "Sin comprobaciones" not in page.text
    assert "Estamos mostrando el último estado conocido" not in page.text

    # Desactivada: las tres rutas públicas son un 404 como cualquier otra.
    stack.stop_hub()
    stack.start_hub(DYNDNSARR_HUB_PUBLIC_ENABLED="false")
    missing = httpx.get(f"{base}/no-such-route", timeout=5)
    for path in ("/", "/public/v1/status", "/static/assets/dyno-green.png"):
        response = httpx.get(f"{base}{path}", timeout=5)
        assert (response.status_code, response.content) == (404, missing.content), path
    assert httpx.get(f"{base}/healthz", timeout=5).status_code == 200


def test_private_hub_https_login_config_command_restart_and_logout(stack):
    """Navegador HTTP simulado, con cookies Secure y ambas conexiones TLS verificadas."""
    from pathlib import Path

    from dyndnsarr_hub.auth.sessions import COOKIE

    password = "contraseña e2e de administrador"
    created = subprocess.run(
        [sys.executable, "-m", "dyndnsarr_hub", "admin-create", "admin"],
        input=password + "\n" + password + "\n",
        env=stack.hub_env(),
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0 and password not in created.stdout + created.stderr
    port = free_port()
    origin = f"https://127.0.0.1:{port}"
    stack.start_hub(DYNDNSARR_HUB_ADMIN_ORIGIN=origin)
    stack.start_agent()
    with (stack.tmp / "proxy.log").open("w") as log:
        proxy = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "https_proxy:app",
                "--app-dir",
                str(Path(__file__).parent),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--ssl-certfile",
                str(stack.cert_file),
                "--ssl-keyfile",
                str(stack.key_file),
            ],
            env={**os.environ, "TEST_HUB_UPSTREAM": f"http://127.0.0.1:{stack.hub_port}"},
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        try:
            wait_for_health(proxy, origin, stack.ca_file)
            with httpx.Client(
                base_url=origin, verify=build_ssl_context(stack.ca_file), timeout=15
            ) as browser:
                assert browser.get("/admin/").status_code == 303
                html = browser.get("/admin/login").text
                csrf = re.search(r'name="csrf" value="([^"]+)"', html)[1]
                response = browser.post(
                    "/admin/login",
                    data={"username": "admin", "password": password, "csrf": csrf},
                    headers={"Origin": origin},
                )
                assert response.status_code == 303
                raw_cookie = browser.cookies.get(COOKIE)
                assert "Secure" in response.headers["set-cookie"]
                wait_until(
                    lambda: bool(
                        stack.hub_rows(
                            "SELECT 1 FROM private_agent_snapshot WHERE config IS NOT NULL"
                        )
                    )
                )

                def form(path, action):
                    page = browser.get(path)
                    assert page.status_code == 200
                    body = re.search(
                        r'action="/admin/actions/' + action + r'"[^>]*>(.*?)</form>',
                        page.text,
                        re.S,
                    )[1]
                    return dict(
                        re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', body)
                    )

                body = form("/admin/config", "config")
                body.update(
                    check_interval_seconds="120",
                    verify_retry_seconds="1",
                    verify_timeout_seconds="900",
                    samples_days="30",
                    events_days="365",
                    ip_sources="\n".join(s.url for s in stack.sources),
                    resolvers=stack.fake_dns.resolver,
                    record_count="2",
                    record_0_name="old.example.com",
                    record_0_mode="monitored",
                    record_0_enabled="on",
                    record_0_provider="",
                    record_1_name="m.example.com",
                    record_1_mode="managed",
                    record_1_enabled="on",
                    record_1_provider="dondominio",
                )
                response = browser.post(
                    "/admin/actions/config", data=body, headers={"Origin": origin}
                )
                assert response.status_code == 303
                assert stack.api("GET", "/config").json()["version"] == 1
                again = browser.post("/admin/actions/config", data=body, headers={"Origin": origin})
                assert again.status_code == 303
                assert stack.agent_rows("SELECT count(*) FROM configuration_versions") == [(1,)]
                body = form("/admin/records", "command")
                body["type"] = "check_dns"
                response = browser.post(
                    "/admin/actions/command", data=body, headers={"Origin": origin}
                )
                assert response.status_code == 303
                command_page = response.headers["location"]
                command_id = command_page.rsplit("/", 1)[1]
                wait_until(
                    lambda: (
                        stack.api("GET", "/commands/" + command_id).json()["status"] == "succeeded"
                    )
                )
                refresh = form(command_page, "command-refresh")
                response = browser.post(
                    "/admin/actions/command-refresh", data=refresh, headers={"Origin": origin}
                )
                assert response.status_code == 303
                assert "succeeded" in browser.get(command_page).text
                before = checks_of(stack, "old.example.com")
                stack.stop_hub()
                wait_until(lambda: checks_of(stack, "old.example.com") > before)
                stack.start_hub(DYNDNSARR_HUB_ADMIN_ORIGIN=origin)
                assert browser.get("/admin/").status_code == 200
                assert browser.cookies.get(COOKIE) == raw_cookie
                public = browser.get("/").text + browser.get("/public/v1/status").text
                for secret in (password, stack.api_token, stack.hub_token, DD_KEY, A, "admin"):
                    assert secret not in public
                logout_html = browser.get("/admin/").text
                csrf = re.search(r'name="csrf" value="([^"]+)"', logout_html)[1]
                assert (
                    browser.post(
                        "/admin/logout", data={"csrf": csrf}, headers={"Origin": origin}
                    ).status_code
                    == 303
                )
                browser.cookies.set(COOKIE, raw_cookie)
                assert browser.get("/admin/api/v1/status").status_code == 401
                assert password not in stack.hub_log.read_text()
        finally:
            stop(proxy)
