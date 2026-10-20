import uuid
from datetime import UTC, datetime

import pytest

from dyndnsarr_hub.auth.tokens import generate_token, hash_token
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, control_repo
from dyndnsarr_shared.control import canonical_json, config_hash
from dyndnsarr_shared.db import connect, migrate, transaction

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
EXPIRES = "2026-10-19T12:05:00Z"
H1, H2 = "a" * 64, "b" * 64


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    for agent_id in ("raspberry-home", "raspberry-office"):
        with transaction(conn):
            agents_repo.create_agent(conn, agent_id, hash_token(generate_token()), NOW)
    yield conn
    conn.close()


def test_migration_0003_applies_over_a_0002_database_with_data(tmp_path):
    conn = connect(tmp_path / "hub.db")
    scripts = sorted(f for f in MIGRATIONS.iterdir() if f.name.endswith(".sql"))
    for version, script in enumerate(scripts[:2], start=1):
        sql = script.read_text(encoding="utf-8")
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    with transaction(conn):
        agents_repo.create_agent(conn, "raspberry-home", hash_token("dda_x"), NOW)
        # Datos del esquema anterior, sin usar repositorios del esquema nuevo.
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
            (
                "raspberry-home",
                str(uuid.uuid4()),
                1,
                "ip_changed",
                "2026-10-19T11:00:00Z",
                "2026-10-19T12:00:00Z",
                "home.example.com",
                "{}",
            ),
        )
    assert migrate(conn, MIGRATIONS) == len(scripts)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"desired_config_versions", "observed_agent_state", "commands"} <= tables
    assert conn.execute("SELECT count(*) FROM events").fetchone() == (1,)
    [row] = agents_repo.list_agents(conn)
    assert (row.agent_id, row.api_url, row.has_api_token) == ("raspberry-home", None, False)


def test_desired_versions_are_monotonic_and_independent_per_agent(conn):
    content = {"check_interval_seconds": 120}
    with transaction(conn):
        first = control_repo.add_desired_version(conn, "raspberry-home", content, NOW)
        second = control_repo.add_desired_version(conn, "raspberry-home", content, NOW)
        other = control_repo.add_desired_version(conn, "raspberry-office", content, NOW)
    assert (first.version, second.version, other.version) == (1, 2, 1)
    latest = control_repo.latest_desired(conn, "raspberry-home")
    assert (latest.version, latest.content_hash, latest.content) == (
        2,
        config_hash(content),
        content,
    )
    assert control_repo.latest_desired(conn, "nadie") is None
    stored = conn.execute("SELECT content FROM desired_config_versions LIMIT 1").fetchone()[0]
    assert stored == canonical_json(content)


def test_a_version_cannot_be_duplicated_for_an_agent(conn):
    with transaction(conn):
        control_repo.add_desired_version(conn, "raspberry-home", {}, NOW)
    with pytest.raises(Exception, match="UNIQUE"):
        conn.execute(
            "INSERT INTO desired_config_versions VALUES ('raspberry-home', 1, 'h', '{}', 'x')"
        )


def test_the_api_token_is_stored_but_never_listed(conn):
    agents_repo.set_api_url(conn, "raspberry-home", "https://pi.lan:8001")
    agents_repo.set_api_token(conn, "raspberry-home", "ddh_canary-token")
    assert agents_repo.get_api_target(conn, "raspberry-home") == (
        "https://pi.lan:8001",
        "ddh_canary-token",
    )
    rows = {a.agent_id: a for a in agents_repo.list_agents(conn)}
    assert (rows["raspberry-home"].api_url, rows["raspberry-home"].has_api_token) == (
        "https://pi.lan:8001",
        True,
    )
    assert "ddh_canary-token" not in repr(rows)
    assert agents_repo.get_api_target(conn, "raspberry-office") == (None, None)


def test_unknown_agents_raise(conn):
    for call in (
        lambda: agents_repo.set_api_url(conn, "nadie", "https://x"),
        lambda: agents_repo.set_api_token(conn, "nadie", "t"),
        lambda: agents_repo.get_api_target(conn, "nadie"),
    ):
        with pytest.raises(agents_repo.UnknownAgentError):
            call()


# --- estado observado ---


def observe(conn, version, result="applied", hash=H1, code=None, agent="raspberry-home"):
    control_repo.record_config_result(conn, agent, version, hash, result, code, NOW)
    return control_repo.get_observed(conn, agent)


def test_applied_sets_version_and_hash(conn):
    state = observe(conn, 3)
    assert (state.config_version, state.config_hash, state.last_result) == (3, H1, "applied")
    assert (state.last_attempt_version, state.last_attempt_at) == (3, "2026-10-19T12:00:00Z")


def test_a_rejection_keeps_the_applied_version_and_records_the_attempt(conn):
    observe(conn, 3)
    state = observe(conn, 4, "rejected", H2, "invalid_config")
    assert (state.config_version, state.config_hash) == (3, H1)
    assert (state.last_result, state.last_code, state.last_attempt_version) == (
        "rejected",
        "invalid_config",
        4,
    )


def test_a_lower_version_never_moves_the_state_back(conn):
    observe(conn, 5)
    state = observe(conn, 3, "applied", H2)
    assert (state.config_version, state.config_hash) == (5, H1)
    assert state.last_attempt_version == 5
    state = observe(conn, 2, "rejected", H2, "stale_version")
    assert (state.last_result, state.last_attempt_version) == ("applied", 5)


def test_recording_the_same_result_twice_is_idempotent(conn):
    once = observe(conn, 3)
    twice = observe(conn, 3)
    assert once == twice


def test_state_is_per_agent(conn):
    observe(conn, 3)
    assert control_repo.get_observed(conn, "raspberry-office") is None


def test_reset_returns_to_version_zero_and_allows_reapplying(conn):
    observe(conn, 5)
    control_repo.record_config_reset(conn, "raspberry-home", NOW)
    state = control_repo.get_observed(conn, "raspberry-home")
    assert (state.config_version, state.config_hash, state.last_result) == (0, None, "reset")
    assert state.last_attempt_version is None
    assert observe(conn, 5).config_version == 5


# --- comandos ---


def new_command(conn, agent="raspberry-home", command_id=None):
    command_id = command_id or str(uuid.uuid4())
    with transaction(conn):
        control_repo.create_command(conn, command_id, agent, "sync_ddns", "admin:cli", NOW, EXPIRES)
    return command_id


def test_a_command_starts_pending_and_follows_the_agent(conn):
    command_id = new_command(conn)
    assert control_repo.get_command(conn, command_id).status == "pending"
    control_repo.update_command_from_agent(conn, command_id, "raspberry-home", "received", None)
    assert control_repo.get_command(conn, command_id).status == "sent"
    control_repo.update_command_from_agent(conn, command_id, "raspberry-home", "running", None)
    assert control_repo.get_command(conn, command_id).status == "sent"
    control_repo.update_command_from_agent(
        conn, command_id, "raspberry-home", "succeeded", {"records": {}}
    )
    done = control_repo.get_command(conn, command_id)
    assert (done.status, done.result) == ("succeeded", {"records": {}})


def test_a_final_state_never_regresses_nor_loses_its_result(conn):
    command_id = new_command(conn)
    control_repo.update_command_from_agent(
        conn, command_id, "raspberry-home", "failed", {"code": "busy"}
    )
    control_repo.update_command_from_agent(conn, command_id, "raspberry-home", "received", None)
    control_repo.update_command_from_agent(conn, command_id, "raspberry-home", "succeeded", {})
    done = control_repo.get_command(conn, command_id)
    assert (done.status, done.result) == ("failed", {"code": "busy"})


def test_an_update_from_another_agent_is_ignored(conn):
    command_id = new_command(conn)
    control_repo.update_command_from_agent(conn, command_id, "raspberry-office", "succeeded", {})
    assert control_repo.get_command(conn, command_id).status == "pending"


def test_updating_an_unknown_command_does_nothing(conn):
    control_repo.update_command_from_agent(conn, "x", "raspberry-home", "succeeded", {})
    assert control_repo.get_command(conn, "x") is None
