import pytest
from pydantic import ValidationError

from dyndnsarr_shared.health import HealthResponse


def test_health_response_shape():
    r = HealthResponse(status="ok", component="agent", version="0.1.0")
    assert r.model_dump() == {"status": "ok", "component": "agent", "version": "0.1.0"}


@pytest.mark.parametrize("field,value", [("status", "fine"), ("component", "other")])
def test_health_response_rejects_unknown_values(field, value):
    data = {"status": "ok", "component": "hub", "version": "0.1.0", field: value}
    with pytest.raises(ValidationError):
        HealthResponse(**data)


def test_health_response_forbids_extra_fields():
    with pytest.raises(ValidationError):
        HealthResponse(status="ok", component="hub", version="0.1.0", boot_count=3)
