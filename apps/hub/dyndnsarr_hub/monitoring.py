"""Métricas e incidencias por duración observada; sin E/S ni interpolación de huecos."""

from datetime import timedelta

from dyndnsarr_hub.clock import iso


def metrics(samples, now, hours):
    start = now - timedelta(hours=hours)
    seconds = dict.fromkeys(("healthy", "degraded", "critical"), 0.0)
    incidents, incident = [], None
    samples = sorted(samples)
    for index, (at, state, cap) in enumerate(samples):
        following = samples[index + 1][0] if index + 1 < len(samples) else now
        left, right = max(start, at), min(now, following, at + timedelta(seconds=cap))
        duration = max(0, (right - left).total_seconds())
        if state in seconds:
            seconds[state] += duration
        if incident and (left > incident["end"] or state == "unknown"):
            incidents.append(incident)
            incident = None
        if duration and state in ("degraded", "critical"):
            if incident is None:
                incident = {"start": left, "end": left, "duration_seconds": 0, "recovered": False}
            incident["end"] = right
            incident["duration_seconds"] += duration
        elif incident and duration and state == "healthy":
            incident["recovered"] = left == incident["end"]
            incidents.append(incident)
            incident = None
    if incident:
        incidents.append(incident)
    known = sum(seconds.values())
    total = hours * 3600
    seconds["unknown"] = max(0, total - known)
    for incident in incidents:
        incident["start"], incident["end"] = iso(incident["start"]), iso(incident["end"])
    return {
        "seconds": seconds,
        "known_seconds": known,
        "window_seconds": total,
        "coverage_percent": 100 * known / total,
        "uptime_percent": 100 * seconds["healthy"] / known if known else None,
        "incidents": incidents,
    }
