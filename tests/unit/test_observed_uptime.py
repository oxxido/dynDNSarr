"""El uptime solo usa duración cubierta por observaciones distintas."""

from datetime import UTC, datetime, timedelta

import pytest

from dyndnsarr_hub.monitoring import metrics

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def test_one_hour_in_a_day_is_not_a_full_day_of_availability():
    samples = [(NOW - timedelta(hours=1), "healthy", 3600)]
    result = metrics(samples, NOW, 24)
    assert result["known_seconds"] == 3600
    assert result["uptime_percent"] == 100
    assert result["coverage_percent"] == pytest.approx(100 / 24)
    assert result["seconds"]["unknown"] == 23 * 3600


def test_gaps_unknown_and_window_clipping():
    result = metrics(
        [
            (NOW - timedelta(hours=25), "healthy", 7200),
            (NOW - timedelta(hours=2), "critical", 3600),
            (NOW - timedelta(minutes=90), "unknown", 0),
            (NOW - timedelta(minutes=30), "healthy", 300),
        ],
        NOW,
        24,
    )
    assert result["seconds"]["healthy"] == 3900
    assert result["seconds"]["critical"] == 1800
    assert result["incidents"][0]["recovered"] is False
    assert result["incidents"][0]["duration_seconds"] == 1800


def test_no_data_null_and_confirmed_recovery():
    assert metrics([], NOW, 24)["uptime_percent"] is None
    result = metrics(
        [
            (NOW - timedelta(minutes=10), "degraded", 300),
            (NOW - timedelta(minutes=5), "healthy", 300),
        ],
        NOW,
        24,
    )
    assert result["incidents"][0]["recovered"] is True
