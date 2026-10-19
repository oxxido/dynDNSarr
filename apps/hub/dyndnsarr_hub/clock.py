from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    """Instante en UTC ISO 8601 con sufijo Z (orden lexicográfico = orden cronológico)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
