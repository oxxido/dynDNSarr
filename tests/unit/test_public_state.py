"""Reglas puras de la vista pública (slice 4, D3 y D5): estado, sufijo de IP y barras."""

from datetime import UTC, datetime, timedelta

import pytest

from dyndnsarr_hub.public.state import (
    AgentProjection,
    RecordProjection,
    build_buckets,
    derive_state,
    ip_suffix,
    worst,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
REFRESH = 60


def record(**overrides):
    base = dict(
        name="home.example.com",
        verification="verified",
        since=NOW - timedelta(days=1),
        failures=0,
        next_attempt_at=None,
        last_error_code=None,
    )
    return RecordProjection(**{**base, **overrides})


def projection(records=None, **overrides):
    base = dict(
        agent_id="a",
        refreshed_at=NOW - timedelta(seconds=10),
        ip_status="confirmed",
        public_ip_confirmed_at=NOW - timedelta(minutes=2),
        last_check_at=NOW - timedelta(minutes=2),
        records=tuple(records if records is not None else [record()]),
    )
    return AgentProjection(**{**base, **overrides})


def state(p):
    return derive_state(p, NOW, REFRESH)


def test_healthy():
    assert state(projection()) == "healthy"


def test_no_projection_or_no_records_is_unknown():
    assert state(None) == "unknown"
    assert state(projection(records=[])) == "unknown"


def test_stale_refresh_boundary_is_three_intervals():
    assert state(projection(refreshed_at=NOW - timedelta(seconds=3 * REFRESH))) == "healthy"
    assert state(projection(refreshed_at=NOW - timedelta(seconds=3 * REFRESH + 1))) == "unknown"


@pytest.mark.parametrize("field", ["last_check_at", "public_ip_confirmed_at"])
def test_observation_boundary_is_fifteen_minutes(field):
    assert state(projection(**{field: NOW - timedelta(minutes=15)})) == "healthy"
    assert state(projection(**{field: NOW - timedelta(minutes=15, seconds=1)})) == "unknown"
    assert state(projection(**{field: None})) == "unknown"


def test_mismatch_is_degraded_until_an_hour_then_critical():
    recent = record(verification="mismatch", since=NOW - timedelta(hours=1))
    old = record(verification="mismatch", since=NOW - timedelta(hours=1, seconds=1))
    assert state(projection([recent])) == "degraded"
    assert state(projection([old])) == "critical"


def test_definitive_error_is_critical():
    # failures > 0 sin próximo intento: el error es definitivo (2b).
    assert state(projection([record(failures=2, next_attempt_at=None)])) == "critical"


def test_failures_with_backoff_is_degraded():
    retry = record(failures=1, next_attempt_at=NOW + timedelta(minutes=1))
    assert state(projection([retry])) == "degraded"


def test_uncertain_ip_is_degraded():
    assert state(projection(ip_status="uncertain")) == "degraded"


@pytest.mark.parametrize("verification", ["pending_verification", "unknown"])
def test_unverified_record_is_degraded(verification):
    assert state(projection([record(verification=verification)])) == "degraded"


def test_unknown_wins_over_critical_when_data_is_stale():
    old = record(verification="mismatch", since=NOW - timedelta(days=1))
    assert state(projection([old], last_check_at=None)) == "unknown"


def test_worst_orders_critical_degraded_unknown_healthy():
    assert worst(["healthy", "unknown"]) == "unknown"
    assert worst(["healthy", "unknown", "degraded"]) == "degraded"
    assert worst(["healthy", "critical", "unknown"]) == "critical"
    assert worst(["healthy", "healthy"]) == "healthy"
    assert worst([]) == "unknown"


@pytest.mark.parametrize(
    ("ip", "suffix"),
    [
        ("83.45.12.134", "34"),
        ("83.45.12.7", "7"),
        ("83.45.12.100", "00"),
        ("8.8.8.8", "8"),
        ("10.0.0.5", None),
        ("192.168.1.10", None),
        ("127.0.0.1", None),
        ("224.0.0.1", None),
        ("2001:db8::1", None),
        ("not-an-ip", None),
        ("", None),
        (None, None),
        (1234, None),
    ],
)
def test_ip_suffix(ip, suffix):
    assert ip_suffix(ip) == suffix


def test_buckets_without_samples_are_unknown_and_zero_coverage():
    buckets, coverage = build_buckets([], NOW)
    assert buckets == ["unknown"] * 84
    assert coverage == 0


def test_buckets_take_the_worst_known_state_and_report_coverage():
    last = NOW - timedelta(minutes=5)
    samples = [
        (last, "healthy"),
        (last - timedelta(minutes=1), "degraded"),
        (last - timedelta(minutes=2), "unknown"),
        (NOW - timedelta(hours=3), "healthy"),
        (NOW - timedelta(hours=3, minutes=5), "critical"),
    ]
    buckets, coverage = build_buckets(samples, NOW)
    assert len(buckets) == 84
    assert buckets[83] == "degraded"
    assert buckets[82] == "critical"
    assert buckets.count("unknown") == 82
    assert coverage == pytest.approx(2 / 84)


def test_a_bucket_with_only_unknown_samples_has_no_coverage():
    buckets, coverage = build_buckets([(NOW - timedelta(minutes=5), "unknown")], NOW)
    assert buckets == ["unknown"] * 84
    assert coverage == 0


def test_sample_at_now_goes_to_the_last_bucket_and_outside_the_window_is_ignored():
    buckets, _ = build_buckets(
        [
            (NOW, "healthy"),
            (NOW - timedelta(days=8), "critical"),
            (NOW + timedelta(hours=1), "critical"),
        ],
        NOW,
    )
    assert buckets[83] == "healthy"
    assert buckets.count("unknown") == 83
