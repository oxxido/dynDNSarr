"""Protocolo de emparejamiento: código, MAC y modelos `/pair/v1` (slice 8, ADR-0013)."""

import pytest
import trustme
from pydantic import ValidationError

from dyndnsarr_shared.pairing import (
    CHALLENGE_LABEL,
    CLAIM_LABEL,
    ClaimRequest,
    check_ca_pem,
    code_hash,
    mac,
    normalize_code,
    verify_mac,
)

CODE = "0123456789abcdef0123456789abcdef"


def claim(**changes):
    values = {
        "agent_id": "raspberry-home",
        "api_url": "https://192.168.1.50:8001",
        "api_ca_pem": trustme.CA().cert_pem.bytes().decode(),
        "hub_token": "dda_" + "a" * 43,
        "api_token": "ddh_" + "b" * 43,
        "mac": "0" * 64,
    }
    return {**values, **changes}


def test_code_needs_128_bits_of_hex():
    assert normalize_code(f"  {CODE.upper()}\n") == CODE
    for weak in ("", CODE[:-1], "z" * 32, "correct horse battery staple!!!!"):
        with pytest.raises(ValueError, match="openssl rand -hex 16"):
            normalize_code(weak)


def test_mac_detects_any_change_and_separates_labels():
    good = mac(CODE, CLAIM_LABEL, "a", "b")
    assert verify_mac(CODE, good, CLAIM_LABEL, "a", "b")
    assert not verify_mac(CODE, good, CLAIM_LABEL, "a", "c")
    assert not verify_mac(CODE, good, CLAIM_LABEL, "ab", "")  # sin ambigüedad de separadores
    assert not verify_mac(CODE, good, CHALLENGE_LABEL, "a", "b")
    assert not verify_mac("f" + CODE[1:], good, CLAIM_LABEL, "a", "b")
    altered = ("1" if good[0] == "0" else "0") + good[1:]
    assert not verify_mac(CODE, altered, CLAIM_LABEL, "a", "b")


def test_code_hash_does_not_contain_the_code():
    assert CODE not in code_hash(CODE) and len(code_hash(CODE)) == 64


def test_claim_rejects_bad_tokens_ca_and_extra_fields():
    ClaimRequest.model_validate(claim())
    for bad in (
        {"hub_token": "ddh_" + "a" * 43},
        {"api_token": "dda_" + "b" * 43},
        {"hub_token": "dda_short"},
        {"api_ca_pem": "no es una CA"},
        {"agent_id": "Raspberry"},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            ClaimRequest.model_validate(claim(**bad))


def test_check_ca_pem_rejects_garbage():
    pem = trustme.CA().cert_pem.bytes().decode()
    assert check_ca_pem(pem) == pem
    with pytest.raises(ValueError):
        check_ca_pem("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
