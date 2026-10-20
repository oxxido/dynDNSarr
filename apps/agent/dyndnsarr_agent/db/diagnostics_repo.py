"""Lecturas paginadas de las observaciones existentes; nunca realizan sondeos."""

import base64
import json

from dyndnsarr_shared.control import DiagnosticObservation, DiagnosticPage, RecordDiagnostic


def diagnostics(conn, record_name=None, limit=100, cursor=None):
    before = None
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            ip_max, dns_max, before, scope = value
            if scope != record_name or not all(isinstance(x, int) for x in (ip_max, dns_max)):
                raise ValueError
            if (
                not isinstance(before, list)
                or len(before) != 3
                or not isinstance(before[0], str)
                or before[1] not in ("ip", "dns")
                or not isinstance(before[2], int)
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Cursor no válido") from None
    else:
        ip_max = conn.execute("SELECT coalesce(max(id),0) FROM public_ip_observations").fetchone()[
            0
        ]
        dns_max = conn.execute("SELECT coalesce(max(id),0) FROM dns_checks").fetchone()[0]
    query = (
        "SELECT id, kind, observed_at, record_name, source, ip, error_code FROM ("
        "SELECT id, 'ip' kind, observed_at, NULL record_name, source, ip, error_code "
        "FROM public_ip_observations WHERE id <= ? UNION ALL "
        "SELECT id, 'dns' kind, observed_at, record_name, resolver source, ip, error_code "
        "FROM dns_checks WHERE id <= ?) WHERE 1=1"
    )
    args = [ip_max, dns_max]
    if record_name:
        query += " AND (record_name = ? OR kind = 'ip')"
        args.append(record_name)
    if before:
        query += " AND (observed_at, kind, id) < (?, ?, ?)"
        args.extend(before)
    query += " ORDER BY observed_at DESC, kind DESC, id DESC LIMIT ?"
    rows = conn.execute(query, (*args, limit + 1)).fetchall()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        value = [ip_max, dns_max, [last[2], last[1], last[0]], record_name]
        next_cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    observations = [
        DiagnosticObservation(
            id=r[0],
            kind=r[1],
            observed_at=r[2],
            record_name=r[3],
            source=r[4],
            ips=r[5].split(",") if r[5] else [],
            error_code=r[6],
        )
        for r in rows[:limit]
    ]
    where, params = (" WHERE record_name = ?", (record_name,)) if record_name else ("", ())
    states = conn.execute("SELECT record_name, accepted_at FROM record_state" + where, params)
    records = []
    for name, accepted in states:
        verified = conn.execute(
            "SELECT max(occurred_at) FROM events WHERE type = 'dns_verified' AND record_name = ?",
            (name,),
        ).fetchone()[0]
        records.append(RecordDiagnostic(name=name, accepted_at=accepted, verified_at=verified))
    return DiagnosticPage(observations=observations, records=records, next_cursor=next_cursor)
