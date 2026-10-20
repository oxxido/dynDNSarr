"""Inventario/WoL aislados del DDNS, con persistencia previa a cualquier paquete."""

import asyncio
import logging
import socket
import uuid

from dyndnsarr_agent.db import ddns_repo
from dyndnsarr_agent.integrations.pihole import PiholeClient
from dyndnsarr_agent.scheduler import utc_now
from dyndnsarr_shared.db import transaction
from dyndnsarr_shared.integrations import Device, IntegrationSnapshot

logger = logging.getLogger(__name__)


async def probe(device):
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(str(device.ip), device.probe_port), 1
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, TimeoutError):
        return False


def send_magic(device):
    packet = b"\xff" * 6 + bytes.fromhex(device.mac.replace(":", "")) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (str(device.broadcast), device.port))


def read_snapshot(conn, now=None):
    row = conn.execute("SELECT content FROM integration_snapshot WHERE id=1").fetchone()
    if row:
        return IntegrationSnapshot.model_validate_json(row[0])
    return IntegrationSnapshot(observed_at=ddns_repo.iso(now or utc_now()), pihole="disabled")


class IntegrationWorker:
    def __init__(
        self, conn, config, http, password="", *, clock=utc_now, check=probe, send=send_magic
    ):
        self.conn, self.config, self.clock = conn, config, clock
        self.check, self.send = check, send
        self.pihole = PiholeClient(http, config.pihole, password) if config.pihole else None

    async def wake(self, device_id, operation_id, actor="manual"):
        existing = self.conn.execute(
            "SELECT device_id,status FROM wake_attempts WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if existing:
            if existing[0] != device_id:
                raise ValueError("command_conflict")
            return {"wake_state": existing[1]}
        policy = next((d for d in self.config.devices if d.id == device_id), None)
        if policy is None or not (policy.automatic if actor == "automatic" else policy.manual):
            raise ValueError("device_not_allowed")
        now = self.clock()
        last = self.conn.execute(
            "SELECT requested_at FROM wake_attempts WHERE device_id=? ORDER BY requested_at "
            "DESC LIMIT 1",
            (device_id,),
        ).fetchone()
        if last and (now - ddns_repo.parse_iso(last[0])).total_seconds() < policy.cooldown_seconds:
            return {"code": "cooldown", "wake_state": "unconfirmed"}
        # Reservar antes de await: dos solicitudes concurrentes ven el mismo cooldown.
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO wake_attempts VALUES (?,?,?,?,?)",
                (operation_id, device_id, ddns_repo.iso(now), "unconfirmed", actor),
            )
        reachable = await self.check(policy)
        state = "reachable" if reachable else "sent"
        if not reachable:
            try:
                self.send(policy)
            except OSError:
                state = "unconfirmed"
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE wake_attempts SET status=? WHERE operation_id=?", (state, operation_id)
            )
            ddns_repo.insert_event(
                self.conn,
                "wake_completed",
                self.clock(),
                None,
                {
                    "operation_id": operation_id,
                    "device_id": device_id,
                    "state": state,
                    "origin": actor,
                },
            )
        return {"wake_state": state}

    async def refresh(self):
        now = self.clock()
        previous = read_snapshot(self.conn, now)
        devices, last_success = previous.devices, previous.last_success_at
        health, code = "disabled", None
        if self.pihole:
            try:
                devices = await self.pihole.read(now)
                health, last_success = "ok", ddns_repo.iso(self.clock())
            except Exception:
                health, code = "unavailable", "integration_failed"
                devices = [d.model_copy(update={"state": "unknown"}) for d in devices]
        targets = []
        for policy in self.config.devices:
            reachable = await self.check(policy)
            last = self.conn.execute(
                "SELECT operation_id,requested_at,status FROM wake_attempts WHERE device_id=? "
                "ORDER BY requested_at DESC LIMIT 1",
                (policy.id,),
            ).fetchone()
            wake_state = None
            if last:
                age = (self.clock() - ddns_repo.parse_iso(last[1])).total_seconds()
                wake_state = (
                    "reachable"
                    if reachable
                    else (
                        "booting"
                        if age < policy.boot_seconds and last[2] in ("sent", "booting")
                        else "unconfirmed"
                    )
                )
                self.conn.execute(
                    "UPDATE wake_attempts SET status=? WHERE operation_id=?", (wake_state, last[0])
                )
            targets.append(
                Device(
                    id=policy.id,
                    name=policy.name,
                    ip=policy.ip,
                    mac=policy.mac,
                    state="active" if reachable else "unknown",
                    manual=policy.manual,
                    automatic=policy.automatic,
                    wake_state=wake_state,
                    last_activity_at=ddns_repo.iso(self.clock()) if reachable else None,
                )
            )
        snapshot = IntegrationSnapshot(
            observed_at=ddns_repo.iso(self.clock()),
            last_success_at=last_success,
            pihole=health,
            error_code=code,
            devices=devices,
            targets=targets,
        )
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO integration_snapshot VALUES (1,?)",
                (snapshot.model_dump_json(),),
            )
            if previous.pihole != health:
                ddns_repo.insert_event(
                    self.conn,
                    "integration_health",
                    self.clock(),
                    None,
                    {"integration": "pihole", "state": health},
                )
        return snapshot

    async def run(self, *, recovered=False):
        if recovered:
            for device in self.config.devices:
                if device.automatic:
                    try:
                        await self.wake(device.id, str(uuid.uuid4()), "automatic")
                    except Exception as exc:
                        logger.warning("auto_wake_failed", extra={"error_type": type(exc).__name__})
        failures = 0
        while True:
            try:
                result = await self.refresh()
                failures = failures + 1 if result.error_code else 0
            except Exception as exc:
                failures += 1
                logger.warning(
                    "integration_refresh_failed", extra={"error_type": type(exc).__name__}
                )
            await asyncio.sleep(min(900, self.config.interval_seconds * 2 ** min(failures, 4)))
