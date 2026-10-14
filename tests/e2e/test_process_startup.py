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

SECRET_CANARY = "canary-secret-value-9f3a"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(component, tmp_path, port):
    env = {
        **os.environ,
        component.env_prefix + "HOST": "127.0.0.1",
        component.env_prefix + "PORT": str(port),
        component.env_prefix + "DATA_DIR": str(tmp_path),
        "SOME_API_TOKEN": SECRET_CANARY,
    }
    return subprocess.Popen(
        [sys.executable, "-m", f"dyndnsarr_{component.name}"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for_healthz(proc, port, timeout=15):
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"el proceso terminó con código {proc.returncode}: {proc.stderr.read()}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return response.status, json.load(response)
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    proc.kill()
    pytest.fail("healthz no respondió a tiempo")


def stop(proc) -> str:
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=15)
    # uvicorn termina el proceso con la señal recibida tras un cierre ordenado.
    assert proc.returncode in (0, -signal.SIGTERM)
    assert "Finished server process" in err
    return err


def read_state(db_path) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT key, value FROM app_state"))
    finally:
        conn.close()


def test_component_restarts_with_persisted_state_and_clean_logs(component, tmp_path):
    port = free_port()
    logs = []
    states = []
    for _ in range(2):
        proc = start(component, tmp_path, port)
        status, body = wait_for_healthz(proc, port)
        assert status == 200
        assert body["component"] == component.name
        logs.append(stop(proc))
        states.append(read_state(tmp_path / component.db_filename))

    assert states[1]["instance_id"] == states[0]["instance_id"]
    assert (states[0]["boot_count"], states[1]["boot_count"]) == ("1", "2")

    for log in logs:
        entries = [json.loads(line) for line in log.splitlines()]
        assert "startup" in [e["message"] for e in entries]
        assert SECRET_CANARY not in log
