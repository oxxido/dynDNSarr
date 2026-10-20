"""Vistas privadas de integraciones, prueba de alertas y exportación por lista blanca."""

from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from dyndnsarr_hub.api.admin import render
from dyndnsarr_hub.auth.web import error
from dyndnsarr_hub.clock import parse_instant, utc_now
from dyndnsarr_hub.db import admin_repo, agents_repo, alerts_repo, auth_repo
from dyndnsarr_hub.services.alert_config import Rule
from dyndnsarr_shared.db import transaction
from dyndnsarr_shared.integrations import IntegrationSnapshot

router = APIRouter(prefix="/admin")


def inventory(conn, session, now):
    result = []
    for agent in agents_repo.list_agents(conn):
        if agent.disabled:
            continue
        row = conn.execute(
            "SELECT content,refreshed_at,error_code FROM integration_cache WHERE agent_id=?",
            (agent.agent_id,),
        ).fetchone()
        snapshot = IntegrationSnapshot.model_validate_json(row[0]) if row and row[0] else None
        stale = (
            not row
            or not row[1]
            or now - parse_instant(row[1]) > timedelta(seconds=180)
            or bool(row[2])
        )
        if snapshot and now - parse_instant(snapshot.observed_at) > timedelta(seconds=180):
            stale = True
        result.append(
            {
                "agent_id": agent.agent_id,
                "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
                "stale": stale,
                "operation_id": admin_repo.issue_operation(
                    conn, session.digest, agent.agent_id, "command", now
                ),
            }
        )
    return result


@router.get("/integrations", response_model=None)
async def integrations(request: Request):
    return render(
        request,
        "integrations",
        agents=inventory(request.app.state.db, request.state.session, utc_now()),
        enabled=request.app.state.settings.integrations_enabled,
    )


@router.get("/api/v1/devices", response_model=None)
async def devices(request: Request):
    return JSONResponse(
        {"agents": inventory(request.app.state.db, request.state.session, utc_now())}
    )


@router.get("/alerts", response_model=None)
async def alerts(request: Request):
    state = request.app.state
    token = admin_repo.issue_operation(
        state.db, request.state.session.digest, None, "alert-test", utc_now()
    )
    rows = state.db.execute(
        "SELECT id,channel,phase,severity,created_at,attempts,status,error_code FROM "
        "alert_deliveries ORDER BY created_at DESC,id DESC LIMIT 100"
    ).fetchall()
    return render(
        request,
        "alerts",
        rules=state.alert_config.rules,
        deliveries=rows,
        operation_id=token,
        invalid=state.alert_config_error,
        channels=state.alert_config.webhook is not None or state.alert_config.smtp is not None,
    )


@router.post("/integrations/test-alert", response_model=None)
async def test_alert(request: Request):
    data = dict(request.state.form)
    data.pop("csrf", None)
    if set(data) != {"operation_id"} or not isinstance(data["operation_id"], str):
        return error(400, "Formulario no válido")
    conn, config, now = request.app.state.db, request.app.state.alert_config, utc_now()
    channels = [name for name in ("webhook", "smtp") if getattr(config, name) is not None]
    if not channels:
        return error(409, "No hay canales configurados")
    try:
        with transaction(conn):
            fresh, _ = admin_repo.claim_operation(
                conn,
                data["operation_id"],
                request.state.session.digest,
                None,
                "alert-test",
                data,
                now,
            )
            if fresh:
                rule = Rule(id="test", events=["ip_changed"], channels=channels, severity="info")
                alerts_repo.enqueue(conn, rule, "test:" + data["operation_id"], now, "test")
                admin_repo.operation_reference(conn, data["operation_id"], "test")
                auth_repo.audit(
                    conn,
                    now,
                    f"admin:{request.state.session.user_id}",
                    "alert_test",
                    operation_id=data["operation_id"],
                )
    except admin_repo.ConflictError:
        return error(409, "Operación no válida; recarga la página")
    return RedirectResponse("/admin/alerts", status_code=303)


@router.get("/api/v1/export", response_model=None)
async def export(request: Request):
    params = request.query_params
    try:
        if set(params) - {"agent", "record", "type", "since", "until", "cursor", "limit"}:
            raise ValueError
        limit = int(params.get("limit", "100"))
        if not 1 <= limit <= 100 or len(params.get("cursor", "")) > 2048:
            raise ValueError
        page = admin_repo.history(
            request.app.state.db,
            agent=params.get("agent"),
            record=params.get("record"),
            event_type=params.get("type"),
            since=params.get("since"),
            until=params.get("until"),
            cursor=params.get("cursor"),
            limit=limit,
        )
    except (ValueError, TypeError):
        return error(400, "Filtros no válidos")
    # Exporta metadatos, nunca detalles/payload/cuerpos de config ni tokens.
    fields = {"agent_id", "event_id", "type", "occurred_at", "received_at", "record_name"}
    return JSONResponse(
        {
            "schema_version": 1,
            "events": [{k: v for k, v in row.items() if k in fields} for row in page["entries"]],
            "next_cursor": page["next_cursor"],
        },
        headers={"Content-Disposition": 'attachment; filename="dyndnsarr-diagnostics.json"'},
    )
