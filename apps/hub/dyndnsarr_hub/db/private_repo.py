"""Caché y observaciones privadas. No se usa desde la capa pública."""

import json
from dataclasses import replace
from datetime import timedelta

from dyndnsarr_hub.clock import iso, parse_instant
from dyndnsarr_hub.db import public_repo
from dyndnsarr_hub.public.state import derive_state
from dyndnsarr_shared.control import AgentStatus, EffectiveConfig


def snapshot(conn, agent_id):
    cursor = conn.execute("SELECT * FROM private_agent_snapshot WHERE agent_id = ?", (agent_id,))
    row = cursor.fetchone()
    return dict(zip([d[0] for d in cursor.description], row, strict=True)) if row else {}


def save_snapshot(conn, agent_id, kind, value, error, now):
    assert kind in ("status", "config", "diagnostics")
    conn.execute("INSERT OR IGNORE INTO private_agent_snapshot (agent_id) VALUES (?)", (agent_id,))
    conn.execute(
        f"UPDATE private_agent_snapshot SET {kind}_error = ? WHERE agent_id = ?", (error, agent_id)
    )
    if value is not None:
        conn.execute(
            f"UPDATE private_agent_snapshot SET {kind} = ?, {kind}_at = ? WHERE agent_id = ?",
            (value.model_dump_json(), iso(now), agent_id),
        )
    if kind == "diagnostics" and value is not None:
        conn.execute(
            "UPDATE private_agent_snapshot SET diagnostics_cursor = ? WHERE agent_id = ?",
            (value.next_cursor, agent_id),
        )
        for item in value.observations:
            conn.execute(
                "INSERT OR IGNORE INTO private_observations VALUES (?,?,?,?,?,?)",
                (
                    agent_id,
                    item.kind,
                    item.id,
                    item.observed_at,
                    item.record_name,
                    item.model_dump_json(),
                ),
            )

        add_availability_samples(conn, agent_id, value, now)


def add_availability_samples(conn, agent_id, page, now):
    value = snapshot(conn, agent_id)
    if not value.get("config"):
        return
    config = EffectiveConfig.model_validate_json(value["config"]).content
    if config.dns is None or config.ip_sources is None or config.records is None:
        return
    interval = config.check_interval_seconds or 300
    policy = public_repo.monitoring_policy(conn)
    expected = {
        "ip": len(config.ip_sources.urls),
        "dns": len(config.dns.resolvers) * sum(r.enabled for r in config.records),
    }
    for kind in ("ip", "dns"):
        observations = [o for o in page.observations if o.kind == kind]
        if not observations or not expected[kind]:
            continue
        at = max(o.observed_at for o in observations)
        latest = [o for o in observations if o.observed_at == at]
        state = "unknown" if len(latest) < expected[kind] else "healthy"
        if any(o.error_code or not o.ips for o in latest):
            state = "degraded"
        cap = max(
            0,
            int(
                (
                    parse_instant(at)
                    + timedelta(seconds=min(interval, policy.observation_max_seconds))
                    - now
                ).total_seconds()
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO private_samples VALUES (?,?,?,?,?,?,?)",
            (agent_id, "@" + kind, iso(now), at, state, cap, policy.version),
        )


def add_state_samples(conn, projection, now, refresh_seconds):
    policy = public_repo.monitoring_policy(conn)
    config = snapshot(conn, projection.agent_id).get("config")
    interval = 300
    if config:
        interval = EffectiveConfig.model_validate_json(config).content.check_interval_seconds or 300
    cap = min(interval, policy.observation_max_seconds)
    at = projection.last_check_at
    if at is None or at > now:
        return
    # Se conserva la identidad de la evidencia, pero la cobertura empieza al recogerla:
    # un refresco tardío no rellena retrospectivamente un hueco del Hub.
    coverage = max(0, int((at + timedelta(seconds=cap) - now).total_seconds()))
    states = [("", derive_state(projection, now, refresh_seconds, policy))]
    states += [
        (r.name, derive_state(replace(projection, records=(r,)), now, refresh_seconds, policy))
        for r in projection.records
    ]
    for resource, state in states:
        conn.execute(
            "INSERT OR IGNORE INTO private_samples VALUES (?,?,?,?,?,?,?)",
            (projection.agent_id, resource, iso(now), iso(at), state, coverage, policy.version),
        )
    current = {r.name for r in projection.records}
    previous = conn.execute(
        "SELECT DISTINCT resource FROM private_samples WHERE agent_id = ?", (projection.agent_id,)
    ).fetchall()
    for (resource,) in previous:
        if resource and not resource.startswith("@") and resource not in current:
            conn.execute(
                "INSERT OR IGNORE INTO private_samples VALUES (?,?,?,?,?,?,?)",
                (projection.agent_id, resource, iso(now), None, "unknown", 0, policy.version),
            )


def mark_gap(conn, agent_id, now):
    resources = conn.execute(
        "SELECT DISTINCT resource FROM private_samples WHERE agent_id = ?", (agent_id,)
    ).fetchall()
    version = public_repo.monitoring_policy(conn).version
    for (resource,) in resources:
        conn.execute(
            "INSERT OR IGNORE INTO private_samples VALUES (?,?,?,?,?,?,?)",
            (agent_id, resource, iso(now), None, "unknown", 0, version),
        )


def samples(conn, agent_id, resource, since):
    rows = conn.execute(
        "SELECT observed_at, state, coverage_seconds FROM private_samples WHERE agent_id = ? "
        "AND resource = ? AND observed_at >= ? ORDER BY observed_at",
        (agent_id, resource, iso(since - timedelta(days=1))),
    )
    return [(parse_instant(at), state, cap) for at, state, cap in rows]


def purge(conn, now):
    for table in ("private_samples", "private_observations"):
        conn.execute(
            f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} "
            "WHERE observed_at < ? LIMIT 2000)",
            (iso(now - timedelta(days=30)),),
        )


def observations(conn, agent_id, record=None, limit=100):
    sql = "SELECT detail FROM private_observations WHERE agent_id = ?"
    params = [agent_id]
    if record:
        sql += " AND (record_name = ? OR kind = 'ip')"
        params.append(record)
    sql += " ORDER BY observed_at DESC, kind, observation_id DESC LIMIT ?"
    return [json.loads(r[0]) for r in conn.execute(sql, (*params, limit))]


def counters(conn, agent_id, since):
    events = dict(
        conn.execute(
            "SELECT type, count(*) FROM events WHERE agent_id = ? "
            "AND occurred_at >= ? GROUP BY type",
            (agent_id, iso(since)),
        )
    )
    rows = conn.execute(
        "SELECT detail FROM private_observations WHERE agent_id = ? AND observed_at >= ?",
        (agent_id, iso(since)),
    )
    values = [json.loads(r[0]) for r in rows]
    return {
        "ip_changes": events.get("ip_changed", 0),
        "renewals": events.get("dns_update_succeeded", 0),
        "checks_ok": sum(bool(v["ips"]) and not v["error_code"] for v in values),
        "checks_failed": sum(not v["ips"] or bool(v["error_code"]) for v in values),
    }


def last_ip_change(conn, agent_id):
    return conn.execute(
        "SELECT max(occurred_at) FROM events WHERE agent_id = ? AND type = 'ip_changed'",
        (agent_id,),
    ).fetchone()[0]


def enabled_snapshots(conn):
    rows = conn.execute("SELECT agent_id FROM agents WHERE disabled = 0 ORDER BY agent_id")
    return [(r[0], snapshot(conn, r[0])) for r in rows]


def max_check_interval(conn):
    intervals, missing = [], []
    for agent_id, value in enabled_snapshots(conn):
        if value.get("config"):
            content = EffectiveConfig.model_validate_json(value["config"]).content
            intervals.append(content.check_interval_seconds or 300)
        else:
            missing.append(agent_id)
    return max(intervals, default=0), missing


def update_policy(conn, observation, critical, factor):
    conn.execute(
        "UPDATE public_monitoring_policy SET observation_max_seconds = ?, "
        "critical_mismatch_seconds = ?, stale_refresh_factor = ?, version = version + 1 "
        "WHERE id = 1",
        (observation, critical, factor),
    )


def status_model(value):
    return AgentStatus.model_validate_json(value["status"]) if value.get("status") else None
