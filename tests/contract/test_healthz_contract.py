from fastapi.testclient import TestClient

from dyndnsarr_shared.health import HealthResponse


def test_healthz_matches_shared_contract_exactly(component, tmp_path):
    with TestClient(component.app(component.settings(tmp_path))) as client:
        body = client.get("/healthz").json()
    assert set(body) == {"status", "component", "version"}
    assert HealthResponse.model_validate(body).component == component.name


def test_openapi_schema_of_healthz_is_the_shared_model(component, tmp_path):
    with TestClient(component.app(component.settings(tmp_path))) as client:
        schema = client.get("/openapi.json").json()
    ok = schema["paths"]["/healthz"]["get"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"]["$ref"].endswith("/HealthResponse")
