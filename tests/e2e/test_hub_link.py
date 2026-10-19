"""Agent y Hub como procesos reales en loopback (criterios 1-3 y 6 del slice 3a)."""

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

A = "1.2.3.4"
NAME = "home.example.com"
AGENT_ID = "raspberry-home"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_healthz(proc, port, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"el proceso terminó con código {proc.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    proc.kill()
    pytest.fail("healthz no respondió a tiempo")


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=15)


def rows(db, sql):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def wait_until(check, timeout=15):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        try:
            value = check()
        except sqlite3.OperationalError:
            value = None
        if value:
            return value
        time.sleep(0.05)
    pytest.fail("no se cumplió la condición a tiempo")


class HubProcess:
    def __init__(self, tmp_path):
        self.port = free_port()
        self.data = tmp_path / "hub-data"
        self.db = self.data / "hub.db"
        self.log = tmp_path / "hub.log"
        self.proc = None

    def env(self):
        return {
            **os.environ,
            "DYNDNSARR_HUB_HOST": "127.0.0.1",
            "DYNDNSARR_HUB_PORT": str(self.port),
            "DYNDNSARR_HUB_DATA_DIR": str(self.data),
        }

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "dyndnsarr_hub", *args],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def add_agent(self, agent_id=AGENT_ID) -> str:
        result = self.cli("agent-add", agent_id)
        assert result.returncode == 0, result.stderr
        return next(line for line in result.stdout.splitlines() if line.startswith("dda_"))

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dyndnsarr_hub"],
            env=self.env(),
            stdout=subprocess.DEVNULL,
            stderr=self.log.open("a"),
        )
        wait_for_healthz(self.proc, self.port)

    def stop(self):
        stop(self.proc)

    def received(self):
        return rows(self.db, "SELECT event_id, sequence FROM events ORDER BY sequence")


class AgentProcess:
    def __init__(self, tmp_path, hub, source_a, source_b, dns):
        self.db = tmp_path / "agent-data" / "agent.db"
        self.dir = tmp_path
        self.hub = hub
        config = tmp_path / "agent.toml"
        config.write_text(
            f'[ip_sources]\nurls = ["{source_a.url}", "{source_b.url}"]\n'
            f'[dns]\nresolvers = ["{dns.resolver}"]\n'
            f'[hub]\nurl = "http://127.0.0.1:{hub.port}"\nagent_id = "{AGENT_ID}"\n'
            f'[[records]]\nname = "{NAME}"\nmode = "monitored"\n',
            encoding="utf-8",
        )
        self.config = config
        self.log = tmp_path / "agent.log"
        self.port = free_port()
        self.proc = None

    def env(self, token):
        return {
            **os.environ,
            "DYNDNSARR_AGENT_HOST": "127.0.0.1",
            "DYNDNSARR_AGENT_PORT": str(self.port),
            "DYNDNSARR_AGENT_DATA_DIR": str(self.dir / "agent-data"),
            "DYNDNSARR_AGENT_CONFIG_FILE": str(self.config),
            "DYNDNSARR_AGENT_HUB_TOKEN": token,
        }

    def start(self, token):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dyndnsarr_agent"],
            env=self.env(token),
            stdout=subprocess.DEVNULL,
            stderr=self.log.open("a"),
        )
        wait_for_healthz(self.proc, self.port)

    def stop(self) -> str:
        stop(self.proc)
        err = self.log.read_text()
        assert "Traceback" not in err
        return err

    def status(self, token) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "dyndnsarr_agent", "status"],
            env=self.env(token),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def events(self):
        return rows(self.db, "SELECT event_id, sequence FROM events ORDER BY sequence")

    def pending(self):
        return rows(
            self.db,
            "SELECT count(*) FROM events WHERE delivered_at IS NULL AND rejected_code IS NULL",
        )[0][0]


@pytest.fixture
def world(tmp_path, make_fake_ip_source, fake_dns):
    fake_dns.answers[NAME] = [A]
    hub = HubProcess(tmp_path)
    token = hub.add_agent()
    agent = AgentProcess(tmp_path, hub, make_fake_ip_source(A), make_fake_ip_source(A), fake_dns)
    yield hub, agent, token
    for proc in (agent.proc, hub.proc):
        if proc is not None and proc.poll() is None:
            proc.kill()


def test_events_made_with_the_hub_down_arrive_exactly_once_and_in_order(world):
    hub, agent, token = world
    agent.start(token)  # el Hub aún no está levantado
    wait_until(lambda: len(agent.events()) >= 3)  # agent_started, ip_changed, dns_verified
    log = agent.stop()
    assert "outbox_delivery_failed" in log  # se registra la racha, sin traza
    assert agent.pending() == len(agent.events())
    assert hub.received() == []

    hub.start()
    agent.start(token)  # el arranque hace una pasada inmediata
    produced = agent.events()
    wait_until(lambda: len(hub.received()) >= len(produced))
    wait_until(lambda: agent.pending() == 0)
    received = hub.received()
    assert received[: len(produced)] == produced  # mismo event_id y sequence, en orden
    assert len({event_id for event_id, _ in received}) == len(received)  # una sola vez
    assert "Hub: 0 evento(s) pendiente(s), 0 rechazado(s)" in agent.status(token)
    agent.stop()

    # Otro reinicio: solo se añade lo nuevo (agent_started), sin duplicar lo entregado.
    agent.start(token)
    wait_until(lambda: len(agent.events()) == len(produced) + 1)
    wait_until(lambda: agent.pending() == 0)
    agent.stop()
    assert hub.received() == agent.events()
    listing = hub.cli("events", "--agent", AGENT_ID).stdout
    assert "agent_started" in listing and "ip_changed" in listing


def test_a_bad_token_keeps_the_events_and_the_right_one_delivers_them(world):
    hub, agent, token = world
    hub.start()
    agent.start("dda_token-equivocado")
    wait_until(lambda: len(agent.events()) >= 3)
    wait_until(lambda: "auth_failed" in agent.status("dda_token-equivocado"))
    log = agent.stop()
    assert "auth_failed" in log and "dda_token-equivocado" not in log
    assert hub.received() == [] and agent.pending() == len(agent.events())

    agent.start(token)
    wait_until(lambda: agent.pending() == 0)
    agent.stop()
    assert hub.received() == agent.events()


def test_the_agent_runs_its_ddns_cycle_with_the_hub_unreachable(world):
    hub, agent, token = world  # el Hub nunca se levanta
    agent.start(token)
    wait_until(lambda: len(agent.events()) >= 3)
    assert rows(agent.db, "SELECT verification FROM record_state") == [("verified",)]
    agent.stop()
