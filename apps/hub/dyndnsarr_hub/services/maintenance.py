"""Integraciones y mantenimiento opcionales, independientes del refresco DDNS."""

import asyncio
import logging
from datetime import timedelta

from dyndnsarr_hub.clock import iso, utc_now
from dyndnsarr_hub.db import agents_repo
from dyndnsarr_hub.services import notifications
from dyndnsarr_hub.services.agent_client import AgentClient, build_http_client
from dyndnsarr_shared.db import transaction

logger = logging.getLogger(__name__)


def purge(conn, now, days, receipts_days):
    cutoff = iso(now - timedelta(days=days))
    for table, column, condition in (
        ("events", "received_at", "1=1"),
        ("audit_log", "occurred_at", "1=1"),
        ("alert_deliveries", "created_at", "status!='pending'"),
        ("commands", "created_at", "status IN ('succeeded','failed','expired')"),
        ("alert_signals", "since_at", "phase!='alert'"),
    ):
        conn.execute(
            f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {column}<? "
            f"AND {condition} LIMIT 500)",
            (cutoff,),
        )
    # Tombstones mantienen deduplicación durante toda la ventana de reenvío acordada.
    conn.execute(
        "DELETE FROM event_receipts WHERE rowid IN (SELECT rowid FROM event_receipts "
        "WHERE received_at<? LIMIT 500)",
        (iso(now - timedelta(days=receipts_days)),),
    )


async def refresh_integrations(conn, settings, transport=None, clock=utc_now):
    async with build_http_client(settings.agent_ca_file, transport) as http:
        for agent_id, url, token in agents_repo.api_targets(conn):
            try:
                reply = await AgentClient(http, url, token).integrations()
                content = reply.value.model_dump_json() if reply.value else None
                code = None if content else "integration_unavailable"
            except Exception:
                content, code = None, "integration_unavailable"
            conn.execute(
                "INSERT INTO integration_cache VALUES (?,?,?,?) ON CONFLICT(agent_id) DO UPDATE "
                "SET "
                "content=COALESCE(excluded.content,integration_cache.content),refreshed_at=excluded.refreshed_at,error_code=excluded.error_code",
                (agent_id, content, iso(clock()), code),
            )


async def run(conn, config, settings, transport=None):
    while True:
        try:
            if settings.integrations_enabled:
                await refresh_integrations(conn, settings, transport)
        except Exception as exc:
            logger.warning("integrations_refresh_failed", extra={"error_type": type(exc).__name__})
        try:
            await notifications.deliver(conn, config, settings)
            with transaction(conn):
                purge(conn, utc_now(), settings.retention_days, settings.receipts_days)
        except Exception as exc:
            logger.warning("maintenance_failed", extra={"error_type": type(exc).__name__})
        await asyncio.sleep(15)
