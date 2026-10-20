import pytest

from dyndnsarr_shared.outbound import DestinationError, allowed_address, validate_url


@pytest.mark.parametrize("address", ["169.254.169.254", "0.0.0.0", "224.0.0.1", "::", "fe80::1"])
def test_sensitive_addresses_never_allowed(address):
    with pytest.raises(DestinationError):
        allowed_address(address, ["0.0.0.0/0", "::/0"])


def test_private_destinations_require_explicit_allowlist():
    with pytest.raises(DestinationError):
        allowed_address("192.168.1.10", [])
    assert allowed_address("192.168.1.10", ["192.168.1.0/24"]) == "192.168.1.10"
    assert allowed_address("1.1.1.1", []) == "1.1.1.1"


@pytest.mark.parametrize("url", ["file:///secret", "https://user:pass@host/", "https://host/#x"])
def test_outbound_urls_reject_credentials_and_unsupported_schemes(url):
    with pytest.raises(DestinationError):
        validate_url(url)


def test_http_requires_explicit_local_permission():
    with pytest.raises(DestinationError):
        validate_url("http://192.168.1.10")
    assert validate_url("http://192.168.1.10", allow_http=True)
