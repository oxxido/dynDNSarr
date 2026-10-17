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
USER, KEY = "canary-user", "canary-key-0123456789"


class Agent:
    """Un Agent real (proceso) con servidores falsos, sin Hub."""

    def __init__(self, tmp_path, source_a, source_b, dns, dondominio=None):
        self.dir = tmp_path
        self.db = tmp_path / "data" / "agent.db"
        self.config = tmp_path / "agent.toml"
        record = 'mode = "monitored"\n'
        extra = ""
        if dondominio is not None:
            record = 'mode = "managed"\nprovider = "dondominio"\n'
            extra = (
                "[update]\nverify_retry_seconds = 1\nverify_timeout_seconds = 30\n"
                f'[providers.dondominio]\nurl = "{dondominio.url}"\n'
            )
        self.config.write_text(
            f'[ip_sources]\nurls = ["{source_a.url}", "{source_b.url}"]\n'
            f'[dns]\nresolvers = ["{dns.resolver}"]\n'
            f'[[records]]\nname = "{NAME}"\n{record}{extra}',
            encoding="utf-8",
        )
        self.credentials = (
            {"DYNDNSARR_AGENT_DONDOMINIO_USER": USER, "DYNDNSARR_AGENT_DONDOMINIO_KEY": KEY}
            if dondominio is not None
            else {}
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
            **self.credentials,
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

    def wait_until(self, sql, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        rows = None
        while time.monotonic() < deadline:
            try:
                rows = self.query(sql)
            except sqlite3.OperationalError:
                rows = None
            if rows is not None and predicate(rows):
                return rows
            time.sleep(0.05)
        pytest.fail(f"{sql!r}: no se cumplió la condición, hay {rows}")

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


# --- slice 2b: registros managed contra un DonDominio falso, sin Hub ---


@pytest.fixture
def managed(tmp_path, make_fake_ip_source, fake_dns, fake_dondominio):
    source_a, source_b = make_fake_ip_source(A), make_fake_ip_source(A)
    fake_dns.answers[NAME] = [A]
    agent = Agent(tmp_path, source_a, source_b, fake_dns, fake_dondominio)
    return agent, source_a, source_b, fake_dns, fake_dondominio


def set_ip(sources, ip):
    for source in sources:
        source.ip = ip


def count(agent, sql):
    return agent.query(sql)[0][0]


def test_ip_change_is_one_update_then_pending_then_verified(managed):
    agent, source_a, source_b, dns, dondominio = managed
    agent.run_once(1)
    assert dondominio.calls == []  # el DNS ya era correcto: ninguna llamada
    assert agent.events() == ["agent_started", "ip_changed", "dns_verified"]

    set_ip((source_a, source_b), B)  # la IP cambia y el DNS tarda en propagarse
    dondominio.propagate = False
    agent.start()
    agent.wait_for("SELECT verification FROM record_state", [("pending_verification",)])
    checks = count(agent, "SELECT count(*) FROM dns_checks")
    agent.wait_until("SELECT count(*) FROM dns_checks", lambda rows: rows[0][0] >= checks + 3)
    assert len(dondominio.calls) == 1  # varios ciclos pendientes, una sola llamada
    assert agent.query("SELECT verification FROM record_state") == [("pending_verification",)]

    dns.answers[NAME] = [B]  # el DNS converge
    agent.wait_for("SELECT verification FROM record_state", [("verified",)])
    agent.stop()
    assert len(dondominio.calls) == 1
    call = dondominio.calls[0]
    assert (call["host"], call["ip"], call["lang"]) == (NAME, B, "en")
    assert agent.events()[3:] == [
        "agent_started",
        "ip_changed",
        "dns_update_succeeded",
        "dns_verified",
    ]
    assert agent.query("SELECT accepted_ip, failures FROM record_state") == [(None, 0)]


def test_no_ip_change_means_no_calls_and_no_new_events(managed):
    agent, *_, dondominio = managed
    agent.run_once(1)
    first = agent.events()
    agent.run_once(2)
    assert dondominio.calls == []
    assert agent.events() == first + ["agent_started"]


def test_provider_down_fails_once_keeps_the_backoff_over_a_restart_and_sync_recovers(managed):
    agent, source_a, source_b, dns, dondominio = managed
    agent.run_once(1)
    set_ip((source_a, source_b), B)
    dondominio.mode = "down"
    agent.start()
    agent.wait_for("SELECT count(*) FROM dns_update_attempts", [(1,)])
    agent.wait_for("SELECT verification, failures FROM record_state", [("mismatch", 1)])
    agent.stop()
    assert agent.events().count("dns_update_failed") == 1
    wait = agent.query("SELECT next_attempt_at FROM record_state")[0][0]
    assert wait is not None

    agent.run_once(3)  # reinicio: la espera pendiente se respeta
    assert len(dondominio.calls) == 1
    assert agent.events().count("dns_update_failed") == 1

    dondominio.mode = "ok"
    sync = agent.command("sync-ddns")  # con el daemon parado
    assert sync.returncode == 0, sync.stderr
    assert "pending_verification" in sync.stdout
    assert len(dondominio.calls) == 2
    agent.start()
    agent.wait_for("SELECT verification FROM record_state", [("verified",)])
    agent.stop()
    assert agent.events().count("dns_update_succeeded") == 1


def test_definitive_error_is_not_retried_until_sync_ddns(managed):
    agent, source_a, source_b, dns, dondominio = managed
    agent.run_once(1)
    set_ip((source_a, source_b), B)
    dondominio.mode = "auth"
    agent.start()
    agent.wait_for("SELECT count(*) FROM dns_update_attempts", [(1,)])
    agent.stop()
    assert agent.query("SELECT failures, next_attempt_at, last_error_code FROM record_state") == [
        (1, None, "auth_failed")
    ]
    agent.run_once(3)
    assert len(dondominio.calls) == 1
    failing = agent.command("sync-ddns")
    assert failing.returncode == 1 and "auth_failed (definitivo)" in failing.stdout
    assert len(dondominio.calls) == 2
    dondominio.mode = "ok"
    assert agent.command("sync-ddns").returncode == 0
    assert len(dondominio.calls) == 3


def test_sync_ddns_does_not_overlap_with_a_running_cycle(managed):
    import fcntl

    agent, source_a, source_b, _, dondominio = managed
    agent.run_once(1)
    set_ip((source_a, source_b), B)
    with open(agent.dir / "data" / "ddns.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = agent.command("sync-ddns")
    assert result.returncode == 1 and "en curso" in result.stderr
    assert dondominio.calls == []


def test_status_shows_the_update_state(managed):
    agent, source_a, source_b, _, dondominio = managed
    agent.run_once(1)
    set_ip((source_a, source_b), B)
    dondominio.mode = "down"
    agent.start()
    agent.wait_for("SELECT count(*) FROM dns_update_attempts", [(1,)])
    agent.stop()
    status = agent.command("status")
    assert status.returncode == 0
    assert f"{NAME}  mismatch" in status.stdout
    assert "reintento a partir de" in status.stdout and "http_error" in status.stdout


def test_the_key_does_not_leak_anywhere(managed):
    agent, source_a, source_b, dns, dondominio = managed
    outputs = [agent.run_once(1)]
    set_ip((source_a, source_b), B)
    dondominio.mode = "down"
    agent.start()
    agent.wait_for("SELECT count(*) FROM dns_update_attempts", [(1,)])
    outputs.append(agent.stop())
    dondominio.mode = "ok"
    for args in (("sync-ddns",), ("status",), ("check-dns",)):
        result = agent.command(*args)
        outputs += [result.stdout, result.stderr]
    agent.start()
    agent.wait_for("SELECT verification FROM record_state", [("verified",)])
    outputs.append(agent.stop())
    assert dondominio.calls and dondominio.calls[0]["password"] == KEY  # la clave sí se envía
    for text in outputs:
        assert KEY not in text and USER not in text
    conn = sqlite3.connect(agent.db)
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert KEY not in dump and USER not in dump
    assert "/json/?" not in dump


def test_managed_record_without_credentials_aborts_startup(managed):
    agent, *_ = managed
    result = subprocess.run(
        [sys.executable, "-m", "dyndnsarr_agent"],
        env={
            **agent.env(),
            "DYNDNSARR_AGENT_DONDOMINIO_USER": "",
            "DYNDNSARR_AGENT_DONDOMINIO_KEY": "",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode not in (0, None)
    assert "DYNDNSARR_AGENT_DONDOMINIO_KEY" in result.stderr
