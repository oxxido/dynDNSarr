"""Adaptador Caddy: bloque reservado, CAS por ETag, sin configuración arbitraria."""

import copy
import ipaddress
import json
from fnmatch import fnmatchcase

import httpx

from dyndnsarr_hub.clock import iso
from dyndnsarr_shared.outbound import DestinationError, resolve


class GatewayError(ValueError):
    def __init__(self, code, *, retry=False):
        super().__init__(code)
        self.code, self.retry = code, retry


def validate_publication(policy, value):
    if policy is None:
        raise GatewayError("gateway_disabled")
    target = policy.destinations.get(value["destination"])
    if value["domain"] not in policy.domains or target is None or value["port"] not in target.ports:
        raise GatewayError("publication_not_allowed")
    if value["access"] == "private" and not policy.private_networks:
        raise GatewayError("private_networks_missing")
    return target


def routes(policy, publications, now):
    result = []
    for p in publications:
        if not p["enabled"] or (p["expires_at"] and p["expires_at"] <= iso(now)):
            continue
        target = validate_publication(policy, p)
        proxy = {"handler": "reverse_proxy", "upstreams": [{"dial": f"{target.host}:{p['port']}"}]}
        if target.scheme == "https":
            proxy["transport"] = {"protocol": "http", "tls": {}}
        handles = [proxy]
        if p["access"] == "private":
            handles = [
                {
                    "handler": "subroute",
                    "routes": [
                        {
                            "match": [{"remote_ip": {"ranges": policy.private_networks}}],
                            "handle": [proxy],
                            "terminal": True,
                        },
                        {"handle": [{"handler": "static_response", "status_code": 403}]},
                    ],
                }
            ]
        result.append(
            {
                "@id": "dda-" + p["publication_id"],
                "match": [{"host": [p["domain"]]}],
                "handle": handles,
                "terminal": True,
            }
        )
    return result


def _replace_owned(servers, server_id, desired):
    """Preserva todos los servidores y rutas ajenos; rechaza capturas/conflictos de host."""
    value = copy.deepcopy(servers)
    server = value.get(server_id, {}) if isinstance(value, dict) else {}
    if server.get("listen") != [":443"] or server.get("automatic_https", {}).get("disable"):
        raise GatewayError("https_server_required")
    existing = server.get("routes", [])
    owned = [r for r in existing if r.get("@id") == "dyndnsarr"]
    if len(owned) != 1 or owned[0] != {
        "@id": "dyndnsarr",
        "handle": [
            {"handler": "subroute", "routes": owned[0].get("handle", [{}])[0].get("routes", [])}
        ],
    }:
        raise GatewayError("owned_block_invalid")
    hosts = [r["match"][0]["host"][0] for r in desired]
    if len(hosts) != len(set(hosts)):
        raise GatewayError("domain_conflict")

    def conflicts(node):
        if isinstance(node, dict):
            if node is owned[0]:
                return False
            if "host" in node and any(
                fnmatchcase(h, pattern) for h in hosts for pattern in node["host"]
            ):
                return True
            return any(conflicts(v) for v in node.values())
        return isinstance(node, list) and any(conflicts(v) for v in node)

    if conflicts(value):
        raise GatewayError("domain_conflict")
    owned[0]["handle"][0]["routes"] = desired
    return value


def replace_owned(servers, server_id, desired):
    try:
        return _replace_owned(servers, server_id, desired)
    except (AttributeError, IndexError, KeyError, TypeError):
        raise GatewayError("owned_block_invalid") from None


class CaddyClient:
    def __init__(self, http, policy, token=None):
        self.http, self.policy, self.token = http, policy, token

    async def request(self, method, suffix, body=None, etag=None):
        url = httpx.URL(self.policy.admin_url + suffix)
        try:
            address = await resolve(
                url.host,
                url.port or (443 if url.scheme == "https" else 80),
                self.policy.admin_networks,
            )
            if url.scheme == "http" and not ipaddress.ip_address(address).is_loopback:
                raise GatewayError("insecure_admin")
            headers = {"Host": url.netloc.decode()}
            if etag:
                headers["If-Match"] = etag
            if self.token:
                headers["Authorization"] = "Bearer " + self.token.get_secret_value()
            async with self.http.stream(
                method,
                url.copy_with(host=address),
                headers=headers,
                json=body,
                extensions={"sni_hostname": url.host},
                follow_redirects=False,
                timeout=5,
            ) as response:
                if response.status_code == 412:
                    raise GatewayError("caddy_conflict", retry=True)
                if not 200 <= response.status_code < 300:
                    raise GatewayError("caddy_rejected", retry=response.status_code >= 500)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise GatewayError("caddy_response_large", retry=True)
                    chunks.append(chunk)
                content = b"".join(chunks)
                return (json.loads(content) if content else None), response.headers.get("etag")
        except GatewayError:
            raise
        except (httpx.HTTPError, OSError, ValueError, DestinationError):
            raise GatewayError("caddy_unavailable", retry=True) from None

    async def apply(self, desired):
        path = "/config/apps/http/servers"
        current, etag = await self.request("GET", path)
        proposed = replace_owned(current, self.policy.server, desired)
        if proposed == current:
            return
        if not etag:
            raise GatewayError("caddy_etag_missing")
        await self.request(
            "PATCH",
            path + "/" + self.policy.server + "/routes",
            proposed[self.policy.server]["routes"],
            etag,
        )
