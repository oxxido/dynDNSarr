"""Hub y Agent como procesos reales que se emparejan con el mismo código (slice 8, criterio 4).

En los dos órdenes de arranque, sin `agent-add`, `agent-set-api` ni `agent-api-token`: el agente
aparece en `agent-list`, sus eventos llegan por el puerto de enlace HTTPS y `agent-status` responde
verificando la CA que el Agent entregó al emparejar.
"""

import os
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
import trustme

pytestmark = pytest.mark.e2e

A = "83.45.12.134"
CODE = "00112233445566778899aabbccddeeff"
HUB_TOKEN = "dda_" + "h" * 43
API_TOKEN = "ddh_" + "a" * 43


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
        except sqlite3.OperationalError:
            value = None
        if value:
            return value
        time.sleep(0.2)
    pytest.fail("no se cumplió la condición a tiempo")


def wait_for_health(proc, url, context=None):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"el proceso terminó con código {proc.returncode}")
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=1, context=context):
                return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    pytest.fail("healthz no respondió a tiempo")


class Pair:
    def __init__(self, tmp_path, make_fake_ip_source, fake_dns):
        fake_dns.answers["home.example.com"] = [A]
        sources = [make_fake_ip_source(A), make_fake_ip_source(A)]
        ca = trustme.CA()
        cert = ca.issue_cert("127.0.0.1")
        self.ca_file, cert_file, key_file = (tmp_path / n for n in ("ca.pem", "c.pem", "k.pem"))
        ca.cert_pem.write_to_path(self.ca_file)
        cert.cert_chain_pems[0].write_to_path(cert_file)
        cert.private_key_pem.write_to_path(key_file)
        self.hub_port, self.link_port, self.agent_port = free_port(), free_port(), free_port()
        self.hub_db = tmp_path / "hub" / "hub.db"
        self.hub_log, self.agent_log = tmp_path / "hub.log", tmp_path / "agent.log"
        config = tmp_path / "agent.toml"
        config.write_text(
            f'[ip_sources]\nurls = ["{sources[0].url}", "{sources[1].url}"]\n'
            f'[dns]\nresolvers = ["{fake_dns.resolver}"]\n'
            f'[hub]\nurl = "https://127.0.0.1:{self.link_port}"\nagent_id = "raspberry-home"\n'
            '[[records]]\nname = "home.example.com"\nmode = "monitored"\n',
            encoding="utf-8",
        )
        self.hub_env = {
            **os.environ,
            "DYNDNSARR_HUB_HOST": "127.0.0.1",
            "DYNDNSARR_HUB_PORT": str(self.hub_port),
            "DYNDNSARR_HUB_DATA_DIR": str(tmp_path / "hub"),
            "DYNDNSARR_HUB_LINK_NAMES": "127.0.0.1,localhost",
            "DYNDNSARR_HUB_LINK_PORT": str(self.link_port),
            "DYNDNSARR_HUB_PAIRING_CODE": CODE,
        }
        self.agent_env = {
            **os.environ,
            "DYNDNSARR_AGENT_HOST": "127.0.0.1",
            "DYNDNSARR_AGENT_PORT": str(self.agent_port),
            "DYNDNSARR_AGENT_DATA_DIR": str(tmp_path / "agent"),
            "DYNDNSARR_AGENT_CONFIG_FILE": str(config),
            "DYNDNSARR_AGENT_PAIRING_CODE": CODE,
            "DYNDNSARR_AGENT_HUB_TOKEN": HUB_TOKEN,
            "DYNDNSARR_AGENT_API_TOKEN": API_TOKEN,
            "DYNDNSARR_AGENT_TLS_CERT_FILE": str(cert_file),
            "DYNDNSARR_AGENT_TLS_KEY_FILE": str(key_file),
            "DYNDNSARR_AGENT_TLS_CA_FILE": str(self.ca_file),
        }
        self.procs = []

    def start_hub(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "dyndnsarr_hub"],
            env=self.hub_env,
            stdout=subprocess.DEVNULL,
            stderr=self.hub_log.open("a"),
        )
        self.procs.append(proc)
        wait_for_health(proc, f"http://127.0.0.1:{self.hub_port}")
        return proc

    def start_agent(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "dyndnsarr_agent"],
            env=self.agent_env,
            stdout=subprocess.DEVNULL,
            stderr=self.agent_log.open("a"),
        )
        self.procs.append(proc)
        context = ssl.create_default_context(cafile=str(self.ca_file))
        wait_for_health(proc, f"https://127.0.0.1:{self.agent_port}", context)
        return proc

    def hub_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "dyndnsarr_hub", *args],
            env=self.hub_env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def agent_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "dyndnsarr_agent", *args],
            env=self.agent_env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def hub_events(self):
        conn = sqlite3.connect(self.hub_db)
        try:
            return [r[0] for r in conn.execute("SELECT type FROM events ORDER BY sequence")]
        finally:
            conn.close()

    def check_linked(self, timeout):
        listed = wait_until(
            lambda: (out := self.hub_cli("agent-list").stdout).startswith("raspberry-home") and out,
            timeout,
        )
        assert f"api: https://127.0.0.1:{self.agent_port}" in listed
        assert "agent_started" in wait_until(self.hub_events, 15)
        status = self.hub_cli("agent-status", "raspberry-home")
        assert status.returncode == 0, status.stderr + status.stdout
        assert self.agent_cli("pair-status").returncode == 0
        # Ni el código ni los tokens en logs o en la consola.
        text = self.hub_log.read_text() + self.agent_log.read_text() + listed + status.stdout
        assert all(secret not in text for secret in (CODE, HUB_TOKEN, API_TOKEN))

    def close(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=15)


@pytest.fixture
def pair(tmp_path, make_fake_ip_source, fake_dns):
    stack = Pair(tmp_path, make_fake_ip_source, fake_dns)
    yield stack
    stack.close()


def test_hub_first_then_agent(pair):
    hub = pair.start_hub()
    pair.start_agent()
    pair.check_linked(timeout=20)
    # Con dos servidores en el proceso, SIGTERM los cierra a los dos.
    hub.send_signal(signal.SIGTERM)
    hub.wait(timeout=15)


def test_agent_first_then_hub(pair):
    pair.start_agent()
    wait_until(lambda: "connect_error" in pair.agent_cli("pair-status").stdout, 20)
    pair.start_hub()
    # El primer reintento llega a los 30 s (±20 %).
    pair.check_linked(timeout=60)
