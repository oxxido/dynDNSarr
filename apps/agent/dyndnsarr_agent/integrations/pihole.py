"""Pi-hole v6: sesión solo en memoria y tres consultas de solo lectura."""

from datetime import UTC, datetime

from dyndnsarr_shared.integrations import Device
from dyndnsarr_shared.outbound import request


def instant(timestamp):
    return (
        datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if timestamp else None
    )


def inventory(network, leases, now):
    """Una consulta DNS reciente es evidencia probable; DHCP nunca confirma conexión."""
    rows = {}
    for device in network["devices"][:500]:
        for address in device["ips"][:10]:
            ip = address["ip"]
            last = device.get("lastQuery", 0)
            age = now.timestamp() - last if last else None
            rows[ip] = Device(
                id="pihole:" + ip,
                name=address.get("name") or ip,
                ip=ip,
                mac=device.get("hwaddr"),
                state="likely_active"
                if age is not None and 0 <= age <= 300
                else ("stale" if last else "known"),
                last_activity_at=instant(last),
            )
            if len(rows) >= 500:
                break
        if len(rows) >= 500:
            break
    for lease in leases["leases"][:500]:
        ip = lease["ip"]
        if ip not in rows and len(rows) < 500:
            rows[ip] = Device(
                id="pihole:" + ip,
                name=lease.get("name") or ip,
                ip=ip,
                mac=lease.get("hwaddr"),
                state="known",
            )
        if ip in rows:
            rows[ip].lease_expires_at = instant(lease["expires"])
    return list(rows.values())


class PiholeClient:
    def __init__(self, http, config, password):
        self.http, self.config, self.password = http, config, password
        self.sid = None

    async def _call(self, method, path, body=None):
        return await request(
            self.http,
            method,
            self.config.url.rstrip("/") + "/api" + path,
            networks=self.config.networks,
            headers={"sid": self.sid} if self.sid else {},
            body=body,
        )

    async def read(self, now):
        if self.sid is None:
            auth = await self._call("POST", "/auth", {"password": self.password})
            if auth["session"]["valid"] is not True:
                raise ValueError("auth_failed")
            self.sid = auth["session"]["sid"]
        try:
            network = await self._call("GET", "/network/devices?max_devices=500&max_addresses=10")
            leases = await self._call("GET", "/dhcp/leases")
            return inventory(network, leases, now)
        except Exception:
            self.sid = None
            raise
