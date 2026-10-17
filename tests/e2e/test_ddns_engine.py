import json
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

A, B = "1.2.3.4", "5.6.7.8"
NAME = "home.example.com"


class Agent:
    """Un Agent real (proceso) con servidores falsos, sin Hub."""

    def __init__(self, tmp_path, source_a, source_b, dns):
        self.dir = tmp_path
        self.db = tmp_path / "data" / "agent.db"
        self.config = tmp_path / "agent.toml"
        self.config.write_text(
            f'[ip_sources]\nurls = ["{source_a.url}", "{source_b.url}"]\n'
            f'[dns]\nresolvers = ["{dns.resolver}"]\n'
            f'[[records]]\nname = "{NAME}"\nmode = "monitored"\n',
            encoding="utf-8",
        )
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = None

    def env(self, **extra):
        return {
            **os.environ,
            "DYNDNSARR_AGENT_HOST": "127.0.0.1",
            "DYNDNSARR_AGENT_PORT": str(self.port),
            "DYNDNSARR_AGENT_DATA_DIR": str(self.dir / "data"),
            "DYNDNSARR_AGENT_CONFIG_FILE": str(self.config),
            **extra,
        }

    def command(self, *args, **env):
        return subprocess.run(
            [sys.executable, "-m", "dyndnsarr_agent", *args],
            env=self.env(**env),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dyndnsarr_agent"],
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"el Agent terminó ({self.proc.returncode}): {self.proc.stderr.read()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=1):
                    return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        self.proc.kill()
        pytest.fail("healthz no respondió a tiempo")

    def stop(self) -> str:
        self.proc.send_signal(signal.SIGTERM)
        _, err = self.proc.communicate(timeout=15)
        assert self.proc.returncode in (0, -signal.SIGTERM)
        assert "Finished server process" in err
        assert "Traceback" not in err and "cycle_failed" not in err
        return err

    def query(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def wait_for(self, sql, expected, timeout=15):
        deadline = time.monotonic() + timeout
        rows = None
        while time.monotonic() < deadline:
            try:
                rows = self.query(sql)
            except sqlite3.OperationalError:
                rows = None
            if rows == expected:
                return
            time.sleep(0.05)
        pytest.fail(f"{sql!r}: se esperaba {expected}, hay {rows}")

    def events(self):
        return [r[0] for r in self.query("SELECT type FROM events ORDER BY sequence")]

    def run_once(self, until_checks):
        """Arranca, espera a que haya `until_checks` comprobaciones DNS en total y para."""
        self.start()
        self.wait_for("SELECT count(*) FROM dns_checks", [(until_checks,)])
        return self.stop()


@pytest.fixture
def world(tmp_path, make_fake_ip_source, fake_dns):
    source_a, source_b = make_fake_ip_source(A), make_fake_ip_source(A)
    fake_dns.answers[NAME] = [A]
    agent = Agent(tmp_path, source_a, source_b, fake_dns)
    return agent, source_a, source_b, fake_dns


def test_ip_change_is_reported_once_and_record_goes_to_mismatch(world):
    agent, source_a, source_b, _ = world
    agent.run_once(1)
    assert agent.query("SELECT verification FROM record_state") == [("verified",)]
    assert agent.events() == ["agent_started", "ip_changed", "dns_verified"]

    source_a.ip = source_b.ip = B  # la IP cambia; el DNS sigue apuntando a A
    agent.run_once(2)  # el reinicio hace la comprobación inmediata
    assert agent.query("SELECT verification FROM record_state") == [("mismatch",)]
    assert agent.events() == [
        "agent_started",
        "ip_changed",
        "dns_verified",
        "agent_started",
        "ip_changed",
        "dns_mismatch",
    ]
    payloads = agent.query("SELECT payload FROM events WHERE type = 'ip_changed' ORDER BY sequence")
    assert [json.loads(p[0]) for p in payloads] == [
        {"previous": None, "ip": A},
        {"previous": A, "ip": B},
    ]


def test_discrepancy_is_uncertain_once_and_resumes_when_sources_agree(world):
    agent, source_a, source_b, _ = world
    agent.run_once(1)
    source_b.ip = B
    agent.run_once(2)
    assert agent.events()[-2:] == ["agent_started", "ip_uncertain"]
    agent.run_once(3)  # sigue discrepando: no se repite el evento
    assert agent.events()[-1] == "agent_started"
    assert agent.events().count("ip_uncertain") == 1
    assert agent.query("SELECT value FROM app_state WHERE key = 'public_ip'") == [(A,)]

    source_b.ip = A  # vuelven a coincidir con la misma IP: se retoma sin ip_changed
    agent.run_once(4)
    assert agent.events().count("ip_changed") == 1
    assert agent.query("SELECT value FROM app_state WHERE key = 'ip_status'") == [("confirmed",)]


def test_one_source_down_is_uncertain(world):
    agent, _, source_b, _ = world
    source_b.ip = None
    agent.run_once(1)
    assert agent.events() == ["agent_started", "ip_uncertain"]


def test_restart_keeps_history_and_checks_immediately(world):
    agent, *_ = world
    agent.run_once(1)
    first = agent.query("SELECT * FROM events ORDER BY sequence")
    agent.run_once(2)
    assert agent.query("SELECT * FROM events ORDER BY sequence")[: len(first)] == first
    assert agent.query("SELECT count(*) FROM public_ip_observations") == [(4,)]


def test_cli_status_check_ip_and_check_dns_against_the_fakes(world):
    agent, *_ = world
    agent.run_once(1)
    status = agent.command("status")
    assert status.returncode == 0
    assert f"IP pública: {A} (confirmada" in status.stdout
    assert f"{NAME}  verified" in status.stdout
    check_ip = agent.command("check-ip")
    assert check_ip.returncode == 0 and f"IP pública confirmada: {A}" in check_ip.stdout
    check_dns = agent.command("check-dns")
    assert check_dns.returncode == 0 and f"{NAME}  verified" in check_dns.stdout


@pytest.mark.parametrize(
    "edit,fragment",
    [
        (lambda t: t.replace("monitored", "managed"), "provider"),
        (lambda t: t.replace("[dns]", "[dns]\nbogus = 1"), "bogus"),
    ],
)
def test_invalid_config_aborts_startup_with_clear_message(world, edit, fragment):
    agent, *_ = world
    agent.config.write_text(edit(agent.config.read_text()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "dyndnsarr_agent"],
        env=agent.env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode not in (0, None)
    assert fragment in result.stderr


def test_missing_config_file_aborts_startup(world):
    agent, *_ = world
    result = subprocess.run(
        [sys.executable, "-m", "dyndnsarr_agent"],
        env=agent.env(DYNDNSARR_AGENT_CONFIG_FILE=str(agent.dir / "nope.toml")),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0 and "no existe" in result.stderr
