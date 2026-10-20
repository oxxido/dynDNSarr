"""Forma fija del `PublicStatusDTO` (slice 4): lo que sale por la vista pública, campo a campo."""

from dyndnsarr_hub.public.dto import PublicStatusDTO


def schema():
    return PublicStatusDTO.model_json_schema(by_alias=True)


def test_top_level_fields_are_exactly_these():
    assert set(schema()["properties"]) == {
        "schema_version",
        "generated_at",
        "state",
        "last_successful_check_at",
        "dns",
        "checks",
        "renewals",
        "history",
    }
    assert schema()["additionalProperties"] is False


def test_nested_fields_and_closed_vocabularies():
    defs = schema()["$defs"]
    assert set(defs["Renewal"]["properties"]) == {
        "occurred_at",
        "ip_suffix",
        "verification",
        "record",
    }
    assert defs["Renewal"]["properties"]["ip_suffix"]["pattern"] == "^[0-9]{1,2}$"
    assert set(defs["History"]["properties"]) == {"from", "bucket_seconds", "buckets", "coverage"}
    assert set(defs["Check"]["properties"]) == {"id", "state"}
    assert defs["Check"]["properties"]["id"]["enum"] == [
        "dns_resolution",
        "ipv4",
        "ipv6",
        "service",
    ]
    assert defs["Check"]["properties"]["state"]["enum"] == [
        "ok",
        "attention",
        "unavailable",
        "unknown",
    ]
    assert schema()["properties"]["state"]["enum"] == [
        "healthy",
        "degraded",
        "critical",
        "unknown",
    ]
    assert all(d["additionalProperties"] is False for d in defs.values())


def test_no_free_form_field_can_carry_an_ip():
    """Ningún campo es un `object` libre ni un string sin patrón salvo los instantes y `record`."""
    defs = schema()["$defs"]
    free_strings = {
        (name, field)
        for name, d in [("DTO", schema()), *defs.items()]
        for field, p in d["properties"].items()
        if p.get("type") == "string" and "pattern" not in p and "enum" not in p
    }
    assert free_strings == {
        ("DTO", "generated_at"),
        ("Renewal", "occurred_at"),
        ("History", "from"),
    }
