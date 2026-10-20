import copy

import pytest
from pydantic import ValidationError

from dyndnsarr_hub.services.caddy import GatewayError, replace_owned
from dyndnsarr_hub.services.service_models import Destination, GatewayPolicy, Grants, Publish


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "100.100.100.200",
        "0.0.0.0",
        "224.0.0.1",
        "fe80::1",
        "metadata.google.internal",
        "1.2.3.4/path",
    ],
)
def test_sensitive_destinations_are_never_accepted(host):
    with pytest.raises((ValidationError, ValueError)):
        Destination(host=host, ports=[8080])


@pytest.mark.parametrize(
    "domains",
    [
        ["*.example.net"],
        ["example.net."],
        ["a..example.net"],
        ["EXAMPLE.NET"],
        ["1.2.3.4"],
        ["-a.example.net"],
    ],
)
def test_delegation_requires_exact_canonical_domains(domains):
    with pytest.raises(ValidationError):
        Grants(actions=["publish"], domains=domains)


def test_admin_requires_tls_except_loopback_and_rejects_paths_credentials():
    base = dict(
        server="gateway",
        domains=["app.example.net"],
        destinations={"app": {"host": "192.168.1.2", "ports": [8080]}},
    )
    for url in (
        "http://192.168.1.1:2019",
        "https://user:secret@admin.test",
        "https://admin.test/path",
    ):
        with pytest.raises(ValidationError):
            GatewayPolicy(admin_url=url, **base)


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        [],
        {},
        {"gateway": None},
        {"gateway": {"listen": [":443"], "routes": [{"@id": "dyndnsarr", "handle": []}]}},
        {"gateway": {"listen": [":443"], "routes": [{"@id": "dyndnsarr", "handle": [{}]}]}},
    ],
)
def test_malformed_caddy_config_is_rejected_without_mutation(malformed):
    before = copy.deepcopy(malformed)
    with pytest.raises(GatewayError):
        replace_owned(malformed, "gateway", [])
    assert malformed == before


def test_schema_does_not_accept_noncanonical_expiry():
    with pytest.raises(ValidationError):
        Publish(
            publication_id="app",
            operation_id="b293f789-6ee4-47ef-ad61-d9e8547fa372",
            expected_revision=0,
            name="test",
            domain="app.example.net",
            destination="app",
            port=8080,
            expires_at="2026-1-1T00:00:00Z",
        )
