"""`PublicStatusDTO` es una lista blanca: rechaza IP completa y campos no declarados."""

import pytest
from pydantic import ValidationError

from dyndnsarr_hub.public.dto import PublicStatusDTO


def dto(**overrides):
    base = {
        "generated_at": "2026-10-19T12:00:00Z",
        "state": "healthy",
        "last_successful_check_at": "2026-10-19T11:58:00Z",
        "dns": "ok",
        "checks": [{"id": "ipv4", "state": "ok"}],
        "renewals": [
            {"occurred_at": "2026-10-19T10:00:00Z", "ip_suffix": "34", "verification": "verified"}
        ],
        "history": {
            "from": "2026-10-12T12:00:00Z",
            "bucket_seconds": 7200,
            "buckets": ["unknown"] * 84,
            "coverage": 0.0,
        },
    }
    return PublicStatusDTO.model_validate({**base, **overrides})


def renewal(**overrides):
    return {
        "occurred_at": "2026-10-19T10:00:00Z",
        "ip_suffix": "34",
        "verification": "verified",
        **overrides,
    }


def test_valid_dto_serializes_history_as_from():
    body = dto().model_dump(mode="json", by_alias=True)
    assert body["history"]["from"] == "2026-10-12T12:00:00Z"
    assert body["schema_version"] == 1
    assert body["renewals"][0]["record"] is None


@pytest.mark.parametrize("suffix", ["83.45.12.134", "192.0.2.134", "34.5", "134", "", "a1", "-1"])
def test_ip_suffix_rejects_anything_but_one_or_two_digits(suffix):
    with pytest.raises(ValidationError):
        dto(renewals=[renewal(ip_suffix=suffix)])


@pytest.mark.parametrize("suffix", ["7", "34", "00"])
def test_ip_suffix_accepts_one_or_two_digits(suffix):
    assert dto(renewals=[renewal(ip_suffix=suffix)]).renewals[0].ip_suffix == suffix


def test_extra_fields_are_rejected_everywhere():
    for extra in (
        {"public_ip": "83.45.12.134"},
        {"renewals": [renewal(ip="83.45.12.134")]},
        {"checks": [{"id": "ipv4", "state": "ok", "detail": "x"}]},
    ):
        with pytest.raises(ValidationError):
            dto(**extra)


def test_at_most_five_renewals():
    assert len(dto(renewals=[renewal()] * 5).renewals) == 5
    with pytest.raises(ValidationError):
        dto(renewals=[renewal()] * 6)


def test_history_needs_exactly_84_buckets():
    history = {"from": "2026-10-12T12:00:00Z", "buckets": ["unknown"] * 83, "coverage": 0}
    with pytest.raises(ValidationError):
        dto(history=history)


@pytest.mark.parametrize("state", ["ok", "green", "", None])
def test_state_is_a_closed_set(state):
    with pytest.raises(ValidationError):
        dto(state=state)
