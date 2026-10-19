import uuid

import pytest
from pydantic import ValidationError

from dyndnsarr_shared.events import (
    AgentEvent,
    ErrorResponse,
    EventBatch,
    EventBatchResult,
    RejectedEvent,
    is_valid_agent_id,
)


def event(**overrides):
    return {
        "event_id": str(uuid.uuid4()),
        "agent_id": "raspberry-home",
        "sequence": 101,
        "type": "dns_update_succeeded",
        "occurred_at": "2026-10-14T15:00:00Z",
        "record_name": "home.example.com",
        "payload": {"result": "verified"},
        **overrides,
    }


def test_event_has_the_architecture_section_5_shape():
    parsed = AgentEvent.model_validate(event())
    assert set(parsed.model_dump()) == {
        "event_id",
        "agent_id",
        "sequence",
        "type",
        "occurred_at",
        "record_name",
        "payload",
    }


def test_record_name_may_be_null():
    assert AgentEvent.model_validate(event(record_name=None)).record_name is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "no-es-uuid"},
        {"event_id": str(uuid.uuid4()).upper()},
        {"agent_id": "Bad_Id"},
        {"agent_id": ""},
        {"sequence": 0},
        {"sequence": "uno"},
        {"type": "Tipo Raro"},
        {"occurred_at": "2026-10-14 15:00:00"},
        {"occurred_at": "2026-10-14T15:00:00+02:00"},
        {"payload": "texto"},
    ],
)
def test_invalid_event_fields_are_rejected(overrides):
    with pytest.raises(ValidationError):
        AgentEvent.model_validate(event(**overrides))


def test_unknown_event_fields_and_types_are_tolerated():
    parsed = AgentEvent.model_validate(event(type="algo_nuevo", novedad=1))
    assert parsed.type == "algo_nuevo"
    assert not hasattr(parsed, "novedad")


def test_batch_and_result_reject_extra_fields():
    with pytest.raises(ValidationError):
        EventBatch.model_validate({"events": [], "otro": 1})
    with pytest.raises(ValidationError):
        EventBatchResult.model_validate({"acked": [], "rejected": [], "otro": 1})


def test_result_shape():
    result = EventBatchResult(
        acked=["a"], rejected=[RejectedEvent(event_id="b", code="invalid_event")]
    )
    assert result.model_dump() == {
        "acked": ["a"],
        "rejected": [{"event_id": "b", "code": "invalid_event"}],
    }


def test_error_response_shape():
    error = ErrorResponse(
        type="auth_error",
        code="unauthorized",
        message_safe="Credenciales no válidas",
        timestamp="2026-10-14T15:00:00Z",
        request_id="r-1",
    )
    assert set(error.model_dump()) == {"type", "code", "message_safe", "timestamp", "request_id"}


@pytest.mark.parametrize(
    ("agent_id", "valid"),
    [
        ("raspberry-home", True),
        ("a", True),
        ("-a", False),
        ("A", False),
        ("a_b", False),
        ("", False),
    ],
)
def test_agent_id_pattern(agent_id, valid):
    assert is_valid_agent_id(agent_id) is valid
