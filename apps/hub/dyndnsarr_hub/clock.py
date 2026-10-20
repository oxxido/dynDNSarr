from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    """Instante en UTC ISO 8601 con sufijo Z (orden lexicográfico = orden cronológico)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(text: str) -> datetime:
    """Inverso de `iso`: instante UTC `YYYY-MM-DDTHH:MM:SSZ`."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
