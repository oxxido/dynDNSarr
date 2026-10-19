import uuid

import pytest
from pydantic import ValidationError

from dyndnsarr_shared.control import (
    AgentHealth,
    AgentStatus,
    CommandRequest,
    CommandStatus,
    ConfigPut,
    ConfigResult,
    canonical_json,
    config_hash,
)

CONTENT = {
    "check_interval_seconds": 120,
    "records": [{"name": "home.example.com", "mode": "monitored"}],
}


def test_config_hash_has_a_fixed_vector():
    # Si este valor cambia, el Hub y el Agent dejan de entenderse: no tocar sin versionar la API.
    assert canonical_json({"b": [1, "é"], "a": True}) == '{"a":true,"b":[1,"é"]}'
    assert config_hash({"b": [1, "é"], "a": True}) == (
        "44c95d186009bf4771bd0c27a0eae65210efd1ef5b32bd8f8c3a2bc376f6814f"
    )


def test_config_hash_ignores_key_order_but_not_content():
    reordered = {"records": CONTENT["records"], "check_interval_seconds": 120}
    assert config_hash(reordered) == config_hash(CONTENT)
    assert config_hash({**CONTENT, "check_interval_seconds": 121}) != config_hash(CONTENT)


def test_config_put_shape():
    put = ConfigPut(version=3, hash=config_hash(CONTENT), content=CONTENT)
    assert set(put.model_dump()) == {"version", "hash", "content"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 0},
        {"hash": "abc"},
        {"hash": "A" * 64},
        {"content": "texto"},
        {"extra": 1},
    ],
)
def test_config_put_rejects_bad_input(overrides):
    base = {"version": 1, "hash": config_hash(CONTENT), "content": CONTENT}
    with pytest.raises(ValidationError):
        ConfigPut.model_validate({**base, **overrides})


def test_config_result_shape():
    applied = ConfigResult(result="applied", version=2)
    rejected = ConfigResult(
        result="rejected", version=2, code="invalid_config", message_safe="dns: falta"
    )
    assert applied.model_dump() == {
        "result": "applied",
        "version": 2,
        "code": None,
        "message_safe": None,
    }
    assert rejected.code == "invalid_config"
    with pytest.raises(ValidationError):
        ConfigResult(result="maybe", version=2)


def command(**overrides):
    return {
        "command_id": str(uuid.uuid4()),
        "type": "check_dns",
        "resource_id": None,
        "requested_by": "admin:1",
        "expires_at": "2026-10-14T15:05:00Z",
        "parameters": {},
        **overrides,
    }


def test_command_request_has_the_architecture_section_5_shape():
    parsed = CommandRequest.model_validate(command())
    assert set(parsed.model_dump()) == {
        "command_id",
        "type",
        "resource_id",
        "requested_by",
        "expires_at",
        "parameters",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"command_id": "no-es-uuid"},
        {"command_id": str(uuid.uuid4()).upper()},
        {"type": "Check DNS"},
        {"requested_by": ""},
        {"expires_at": "2026-10-14 15:05:00"},
        {"expires_at": None},
        {"parameters": "x"},
        {"extra": 1},
    ],
)
def test_command_request_rejects_bad_input(overrides):
    with pytest.raises(ValidationError):
        CommandRequest.model_validate(command(**overrides))


def test_command_status_shape_and_statuses():
    base = {
        "command_id": str(uuid.uuid4()),
        "type": "sync_ddns",
        "status": "received",
        "received_at": "2026-10-14T15:00:00Z",
    }
    status = CommandStatus.model_validate(base)
    assert status.finished_at is None and status.result is None
    with pytest.raises(ValidationError):
        CommandStatus.model_validate({**base, "status": "sent"})


def test_health_and_status_shapes():
    health = AgentHealth(
        agent_id="raspberry-home",
        version="0.1.0",
        status="ok",
        time_utc="2026-10-14T15:00:00Z",
        last_local_dns_check_at=None,
    )
    assert set(health.model_dump()) == {
        "agent_id",
        "version",
        "status",
        "time_utc",
        "last_local_dns_check_at",
    }
    status = AgentStatus(
        agent_id=None,
        version="0.1.0",
        time_utc="2026-10-14T15:00:00Z",
        config_version=0,
        config_hash=None,
        ip_status=None,
        public_ip=None,
        public_ip_confirmed_at=None,
        last_check_at=None,
        records=[],
        hub=None,
    )
    assert status.config_version == 0
