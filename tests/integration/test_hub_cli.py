import sqlite3

import pytest

from dyndnsarr_hub import cli
from dyndnsarr_hub.auth.tokens import hash_token


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DYNDNSARR_HUB_DATA_DIR", str(tmp_path))
    return tmp_path


def token_from(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("dda_"))


def run(capsys, *argv):
    code = cli.run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def tables_text(path) -> str:
    conn = sqlite3.connect(path / "hub.db")
    try:
        parts = []
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            parts += [str(row) for row in conn.execute(f"SELECT * FROM {table}")]
        return "\n".join(parts)
    finally:
        conn.close()


def test_agent_add_prints_the_token_once_and_stores_only_the_hash(env, capsys):
    code, out, _ = run(capsys, "agent-add", "raspberry-home")
    token = token_from(out)
    assert code == 0
    assert token not in tables_text(env)
    assert hash_token(token) in tables_text(env)
    _, listing, _ = run(capsys, "agent-list")
    assert token not in listing and hash_token(token) not in listing
    assert "raspberry-home" in listing and "tokens vigentes: 1" in listing


def test_agent_add_errors(env, capsys):
    assert run(capsys, "agent-add", "raspberry-home")[0] == 0
    code, out, err = run(capsys, "agent-add", "raspberry-home")
    assert code == 1 and "ya existe" in err and "dda_" not in out
    code, out, err = run(capsys, "agent-add", "Mal_Id")
    assert code == 1 and "no válido" in err and "dda_" not in out


def test_rotate_keeps_the_old_token_and_revoke_leaves_the_last(env, capsys):
    run(capsys, "agent-add", "raspberry-home")
    code, out, _ = run(capsys, "agent-rotate", "raspberry-home")
    assert code == 0 and token_from(out)
    assert "tokens vigentes: 2" in run(capsys, "agent-list")[1]
    code, out, _ = run(capsys, "agent-revoke", "raspberry-home")
    assert code == 0 and "1 token(s)" in out
    assert "tokens vigentes: 1" in run(capsys, "agent-list")[1]


def test_revoke_all_disables_the_agent(env, capsys):
    run(capsys, "agent-add", "raspberry-home")
    code, out, _ = run(capsys, "agent-revoke", "raspberry-home", "--all")
    assert code == 0 and "deshabilitado" in out
    assert "deshabilitado  tokens vigentes: 0" in run(capsys, "agent-list")[1]


def test_unknown_agent_is_a_clear_error(env, capsys):
    for command in ("agent-rotate", "agent-revoke"):
        code, _, err = run(capsys, command, "nadie")
        assert code == 1 and "no existe" in err


def test_events_lists_received_events_without_secrets(env, capsys):
    _, out, _ = run(capsys, "agent-add", "raspberry-home")
    token = token_from(out)
    conn = sqlite3.connect(env / "hub.db")
    conn.execute(
        "INSERT INTO events VALUES ('raspberry-home', 'e1', 1, 'ip_changed', "
        "'2026-10-19T11:00:00Z', '2026-10-19T11:00:05Z', NULL, '{\"ip\": \"1.2.3.4\"}')"
    )
    conn.commit()
    conn.close()
    code, listing, _ = run(capsys, "events", "--agent", "raspberry-home")
    assert code == 0
    assert "ip_changed" in listing and "1.2.3.4" in listing
    assert token not in listing and hash_token(token) not in listing
    assert "(sin eventos)" in run(capsys, "events", "--agent", "otro")[1]
