"""Servicios autorizados para API y MCP; no reutilizan sesiones web ni aceptan permisos."""

import json
from datetime import timedelta
from hashlib import sha256

from dyndnsarr_hub.clock import iso, parse_instant, utc_now
from dyndnsarr_hub.db import admin_repo, auth_repo, control_repo, private_repo, services_repo
from dyndnsarr_hub.services import admin_actions, control, gateway
from dyndnsarr_hub.services.agent_client import AgentClient, build_http_client
from dyndnsarr_hub.services.caddy import validate_publication
from dyndnsarr_hub.services.service_models import TOOLS
from dyndnsarr_shared.control import CommandRequest, canonical_json
from dyndnsarr_shared.db import transaction
from dyndnsarr_shared.integrations import IntegrationSnapshot


class ServiceError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def denied(conn, client_id, action, resource, now):
    auth_repo.audit(
        conn, now, "client:" + client_id, action, resource, result="denied", code="forbidden"
    )
    raise ServiceError("forbidden", 403)


def authorize(conn, client_id, grants, action, data, now):
    resource = (
        getattr(data, "domain", None)
        or getattr(data, "publication_id", None)
        or getattr(data, "device_id", None)
    )
    if (
        action not in grants.actions
        or (hasattr(data, "agent_id") and data.agent_id not in grants.agents)
        or (hasattr(data, "domain") and data.domain not in grants.domains)
        or (hasattr(data, "device_id") and data.device_id not in grants.devices)
    ):
        denied(conn, client_id, action, resource, now)


def operation_view(conn, op):
    response = op["response"]
    state = op["state"]
    if op["action"] in ("check_dns", "sync_ddns", "wake_device"):
        command = control_repo.get_command(conn, op["id"])
        if command:
            state = (
                "succeeded"
                if command.status == "succeeded"
                else "failed"
                if command.status in ("failed", "expired")
                else "pending"
            )
            response = {
                "command_id": command.command_id,
                "status": command.status,
                "code": control.command_details(command)["code"],
            }
    return {"operation_id": op["id"], "state": state, "result": response}


def snapshot(conn, agent_id, now, settings):
    value = private_repo.snapshot(conn, agent_id)
    model = private_repo.status_model(value)
    stale = (
        model is None
        or value.get("status_error") is not None
        or not value.get("status_at")
        or now - parse_instant(value["status_at"])
        > timedelta(seconds=3 * settings.status_refresh_seconds)
    )
    return value, model, stale


def publication_view(conn, value, now):
    row = conn.execute(
        "SELECT content FROM publication_checks WHERE publication_id=?", (value["publication_id"],)
    ).fetchone()
    expired = bool(value["expires_at"] and value["expires_at"] <= iso(now))
    return {
        **value,
        "state": "expired" if expired else "configured" if value["enabled"] else "disabled",
        "availability": json.loads(row[0]) if row else {"state": "unknown", "observed_at": None},
    }


def query(conn, client_id, grants, action, data, now, settings):
    if action == "get_operation":
        op = services_repo.operation(conn, data.operation_id)
        if not op or op["client_id"] != client_id:
            raise ServiceError("not_found", 404)
        return operation_view(conn, op)
    if action in ("list_publications", "publication_history"):
        if action == "list_publications":
            return {
                "publications": [
                    publication_view(conn, p, now)
                    for p in services_repo.publications(conn)
                    if gateway.visible(grants, p)
                ]
            }
        # Una publicación eliminada conserva autorización mediante sus revisiones.
        rows = conn.execute(
            "SELECT revision,content,created_at FROM publication_revisions "
            "WHERE publication_id=? ORDER BY revision DESC LIMIT 100",
            (data.publication_id,),
        ).fetchall()
        revisions = [
            {"revision": r, "publication": json.loads(c), "created_at": at}
            for r, c, at in rows
            if c and gateway.visible(grants, json.loads(c))
        ]
        if not revisions:
            raise ServiceError("not_found", 404)
        return {"revisions": revisions}
    if action == "dns_history":
        return admin_repo.history(
            conn, agent=data.agent_id, record=data.domain, cursor=data.cursor, limit=data.limit
        )
    if action == "list_devices":
        row = conn.execute(
            "SELECT content,refreshed_at,error_code FROM integration_cache WHERE agent_id=?",
            (data.agent_id,),
        ).fetchone()
        value = IntegrationSnapshot.model_validate_json(row[0]) if row and row[0] else None
        stale = (
            not value
            or bool(row[2])
            or not row[1]
            or now - parse_instant(row[1]) > timedelta(seconds=180)
            or now - parse_instant(value.observed_at) > timedelta(seconds=180)
        )
        devices = []
        if value:
            for d in value.devices + value.targets:
                if d.id in grants.devices and not any(v["id"] == d.id for v in devices):
                    devices.append(
                        {"id": d.id, "name": d.name, "state": d.state, "wake_state": d.wake_state}
                    )
        return {
            "devices": devices,
            "stale": stale,
            "observed_at": value.observed_at if value else None,
        }
    value, status, stale = snapshot(conn, data.agent_id, now, settings)
    if action == "get_status":
        return {
            "agent_id": data.agent_id,
            "ip_status": status.ip_status
            if status and status.ip_status in ("confirmed", "uncertain")
            else None,
            "last_check_at": status.last_check_at if status else None,
            "stale": stale,
            "observed_at": value.get("status_at"),
        }
    from dyndnsarr_hub.services.agent_client import safe_code

    return {
        "records": [
            {
                **r.model_dump(),
                "last_error_code": safe_code(r.last_error_code),
                "verification": r.verification
                if r.verification
                in ("verified", "mismatch", "pending_verification", "no_reference", "unknown")
                else "unknown",
            }
            for r in status.records
            if r.name in grants.domains
        ]
        if status
        else [],
        "stale": stale,
        "observed_at": value.get("status_at"),
    }


def duplicate(conn, client_id, action, data):
    op = services_repo.operation(conn, data.operation_id)
    if op:
        digest = sha256(canonical_json(data.model_dump(mode="json")).encode()).hexdigest()
        if op["client_id"] != client_id or op["action"] != action or op["fingerprint"] != digest:
            raise ServiceError("operation_conflict", 409)
    return op


async def command(conn, client_id, grants, action, data, settings, transport, now):
    try:
        url, token = admin_actions.target(conn, data.agent_id)
    except admin_actions.ActionError:
        raise ServiceError("agent_unavailable", 409) from None
    op = duplicate(conn, client_id, action, data)
    if op is None:
        if control_repo.get_command(conn, str(data.operation_id)) is not None:
            raise ServiceError("operation_conflict", 409)
        if action == "wake_device":
            if not settings.integrations_enabled:
                raise ServiceError("integrations_disabled", 409)
            inventory = query(conn, client_id, grants, "list_devices", data, now, settings)
            if inventory["stale"] or not any(
                d["id"] == data.device_id for d in inventory["devices"]
            ):
                raise ServiceError("device_unavailable", 409)
        else:
            _, status, stale = snapshot(conn, data.agent_id, now, settings)
            record = (
                next((r for r in status.records if r.name == data.domain), None) if status else None
            )
            if stale or not record:
                raise ServiceError("record_unavailable", 409)
            if action == "sync_ddns" and record.mode != "managed":
                raise ServiceError("monitored_read_only", 403)
        resource = data.device_id if action == "wake_device" else data.domain
        with transaction(conn):
            services_repo.reserve(
                conn,
                data.operation_id,
                client_id,
                action,
                resource,
                data.model_dump(mode="json"),
                now,
            )
            control_repo.create_command(
                conn,
                str(data.operation_id),
                data.agent_id,
                action,
                "client:" + client_id,
                now,
                iso(now + timedelta(minutes=5)),
                resource_id=resource,
            )
    stored = control_repo.get_command(conn, str(data.operation_id))
    if stored.status == "pending":
        request = CommandRequest(
            command_id=stored.command_id,
            type=stored.type,
            requested_by=stored.requested_by,
            resource_id=stored.resource_id,
            expires_at=stored.expires_at,
        )
        async with build_http_client(settings.agent_ca_file, transport) as http:
            await control.send_command(conn, AgentClient(http, url, token), request, data.agent_id)
        stored = control_repo.get_command(conn, stored.command_id)
        if stored.status in ("pending", "sent"):
            with transaction(conn):
                services_repo.finish(
                    conn,
                    data.operation_id,
                    "pending",
                    {"command_id": stored.command_id, "status": stored.status},
                    now,
                )
    return operation_view(conn, services_repo.operation(conn, data.operation_id))


def prepare_publication(conn, client_id, grants, action, data, policy, now):
    revision, current = services_repo.publication(conn, data.publication_id)
    if data.expected_revision != revision:
        raise ServiceError("revision_conflict", 409)
    if current and not gateway.visible(grants, current):
        denied(conn, client_id, action, data.publication_id, now)
    if action == "publish":
        candidate = {
            k: v
            for k, v in data.model_dump(mode="json").items()
            if k not in ("operation_id", "expected_revision")
        }
        candidate.update(
            revision=revision + 1,
            creator=current["creator"] if current else client_id,
            created_at=current["created_at"] if current else iso(now),
            proxy="caddy",
            https="automatic",
        )
        if candidate["expires_at"] and candidate["expires_at"] <= iso(now):
            raise ServiceError("expiration_in_past")
    elif action == "rollback_publication":
        row = conn.execute(
            "SELECT content FROM publication_revisions WHERE publication_id=? AND revision=?",
            (data.publication_id, data.revision),
        ).fetchone()
        if not row or not row[0]:
            raise ServiceError("revision_not_found", 404)
        candidate = json.loads(row[0])
        if candidate["expires_at"] and candidate["expires_at"] <= iso(now):
            raise ServiceError("expiration_in_past")
    else:
        if current is None:
            raise ServiceError("not_found", 404)
        candidate = {**current, "enabled": False} if action == "disable_publication" else None
    if candidate:
        if not gateway.visible(grants, candidate):
            denied(conn, client_id, action, data.publication_id, now)
        if candidate["enabled"]:
            validate_publication(policy, candidate)
        if any(
            p["domain"] == candidate["domain"] and p["publication_id"] != data.publication_id
            for p in services_repo.publications(conn)
        ):
            raise ServiceError("domain_conflict", 409)
    if len(services_repo.publications(conn)) >= 100 and revision == 0:
        raise ServiceError("publication_limit", 409)
    return candidate


async def execute(
    conn,
    client_id,
    grants,
    action,
    data,
    settings,
    policy,
    *,
    agent_transport=None,
    gateway_transport=None,
    clock=utc_now,
):
    now = clock()
    authorize(conn, client_id, grants, action, data, now)
    if TOOLS[action][1] == "read":
        return query(conn, client_id, grants, action, data, now, settings)
    if action == "verify_publication":
        _, value = services_repo.publication(conn, data.publication_id)
        if not value or not gateway.visible(grants, value):
            denied(conn, client_id, action, data.publication_id, now)
        result = await gateway.verify(policy, value, gateway_transport, clock)
        conn.execute(
            "INSERT INTO publication_checks VALUES (?,?) ON CONFLICT(publication_id) "
            "DO UPDATE SET content=excluded.content",
            (data.publication_id, canonical_json(result)),
        )
        auth_repo.audit(
            conn,
            clock(),
            "client:" + client_id,
            action,
            data.publication_id,
            result=result["state"],
            code=result["code"],
        )
        return result
    if action in ("check_dns", "sync_ddns", "wake_device"):
        return await command(conn, client_id, grants, action, data, settings, agent_transport, now)
    if policy is None:
        raise ServiceError("gateway_disabled", 409)
    op = duplicate(conn, client_id, action, data)
    if op is None:
        if conn.execute(
            "SELECT 1 FROM service_operations WHERE state='pending' AND action IN "
            "('publish','disable_publication','delete_publication','rollback_publication')"
        ).fetchone():
            raise ServiceError("gateway_busy", 409)
        candidate = prepare_publication(conn, client_id, grants, action, data, policy, now)
        with transaction(conn):
            services_repo.reserve(
                conn,
                data.operation_id,
                client_id,
                action,
                data.publication_id,
                data.model_dump(mode="json"),
                now,
            )
            # El fingerprint sigue ligado a la petición original; la intención incluye la revisión.
            content = {**data.model_dump(mode="json"), "candidate": candidate}
            conn.execute(
                "UPDATE service_operations SET request=? WHERE id=?",
                (canonical_json(content), str(data.operation_id)),
            )
        op = services_repo.operation(conn, data.operation_id)
    if op["state"] == "pending":
        await gateway.apply(conn, policy, settings, gateway_transport, now, op)
    return operation_view(conn, services_repo.operation(conn, data.operation_id))
