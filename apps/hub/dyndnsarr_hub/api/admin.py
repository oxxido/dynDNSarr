"""HTML y API de administración. Auth/CSRF se aplican antes de estas rutas."""

import json
from dataclasses import asdict, replace
from datetime import timedelta
from urllib.parse import urlencode

import jinja2
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError

from dyndnsarr_hub.auth import sessions
from dyndnsarr_hub.auth.web import error
from dyndnsarr_hub.clock import parse_instant, utc_now
from dyndnsarr_hub.db import admin_repo, auth_repo, control_repo, private_repo, public_repo
from dyndnsarr_hub.monitoring import metrics
from dyndnsarr_hub.public.state import derive_state, worst
from dyndnsarr_hub.services import admin_actions, control, status_refresher
from dyndnsarr_shared.control import EffectiveConfig

router = APIRouter(prefix="/admin")
_templates = jinja2.Environment(
    loader=jinja2.PackageLoader("dyndnsarr_hub", "templates"), autoescape=True
)
_templates.filters["query"] = lambda value: urlencode(value)


def render(request, template, *, status=200, **context):
    return HTMLResponse(
        _templates.get_template("admin/" + template + ".j2").render(
            nonce=request.state.nonce,
            csrf=request.state.csrf,
            path=request.url.path,
            authenticated=bool(request.state.session and request.state.session.user_id),
            **context,
        ),
        status_code=status,
    )


def cookie(response, raw, authenticated=False):
    response.set_cookie(
        sessions.COOKIE,
        raw,
        max_age=43200 if authenticated else 600,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/login", response_model=None)
async def login_form(request: Request):
    if request.state.session and request.state.session.user_id:
        return RedirectResponse("/admin/", status_code=303)
    if not request.app.state.settings.admin_origin:
        return render(
            request,
            "login",
            status=503,
            message="Configura el origen HTTPS de administración antes de iniciar sesión.",
        )
    if not auth_repo.user(request.app.state.db):
        return render(request, "login", message="Falta el aprovisionamiento por consola.")
    if request.state.session is None:
        raw, session = sessions.create(request.app.state.db, utc_now())
        request.state.session, request.state.csrf = session, sessions.csrf(raw)
        return cookie(render(request, "login"), raw)
    return render(request, "login")


@router.post("/login", response_model=None)
async def login(request: Request):
    data = request.state.form
    username, password = data.get("username", ""), data.get("password", "")
    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or len(username) > 64
        or len(password) > 128
    ):
        return error(400, "Credenciales no válidas")
    user_id, wait = sessions.authenticate(request.app.state.db, username, password, utc_now())
    if wait:
        response = render(
            request, "login", status=429, message="Demasiados intentos; espera 15 min."
        )
        response.headers["Retry-After"] = str(wait)
        return response
    if user_id is None:
        return render(request, "login", status=401, message="Credenciales no válidas")
    auth_repo.revoke(request.app.state.db, request.state.session.digest)
    raw, _session = sessions.create(request.app.state.db, utc_now(), user_id)
    return cookie(RedirectResponse("/admin/", status_code=303), raw, True)


@router.post("/logout", response_model=None)
async def logout(request: Request):
    conn, session = request.app.state.db, request.state.session
    auth_repo.revoke(conn, session.digest)
    auth_repo.audit(conn, utc_now(), f"admin:{session.user_id}", "logout", result="succeeded")
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(sessions.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


def summaries(request):
    conn, settings, now = request.app.state.db, request.app.state.settings, utc_now()
    policy = public_repo.monitoring_policy(conn)
    result = []
    for agent_id, value in private_repo.enabled_snapshots(conn):
        status = private_repo.status_model(value)
        projection = (
            status_refresher.project(agent_id, status, parse_instant(value["status_at"]))
            if status
            else None
        )
        effective = (
            EffectiveConfig.model_validate_json(value["config"]) if value.get("config") else None
        )
        configured = {r.name: r for r in effective.content.records or []} if effective else {}
        records = []
        for record in status.records if status else []:
            config_record = configured.get(record.name)
            state = derive_state(
                replace(
                    projection,
                    records=(next(r for r in projection.records if r.name == record.name),),
                ),
                now,
                settings.status_refresh_seconds,
                policy,
            )
            records.append(
                {
                    **record.model_dump(),
                    "state": state,
                    "provider": config_record.provider if config_record else None,
                }
            )
        result.append(
            {
                "agent_id": agent_id,
                "snapshot": value,
                "status": status.model_dump() if status else None,
                "effective": effective.model_dump(exclude_none=True) if effective else None,
                "state": derive_state(projection, now, settings.status_refresh_seconds, policy),
                "records": records,
                "last_ip_change": private_repo.last_ip_change(conn, agent_id),
                "age_seconds": max(
                    0, int((now - parse_instant(value["status_at"])).total_seconds())
                )
                if value.get("status_at")
                else None,
            }
        )
    return result


def context(request, page):
    conn, now = request.app.state.db, utc_now()
    agents = summaries(request)
    selected = request.query_params.get("agent")
    agent = (
        next((a for a in agents if a["agent_id"] == selected), None)
        if selected
        else (agents[0] if agents else None)
    )
    if selected and agent is None and page != "history":
        raise admin_actions.ActionError("Agent desconocido", 404)
    hours = int(request.query_params.get("hours", "24"))
    if hours not in (24, 168, 720):
        raise ValueError
    resource = request.query_params.get("record", "")
    if (
        page != "history"
        and resource
        and (not agent or resource not in {r["name"] for r in agent["records"]})
    ):
        raise admin_actions.ActionError("Registro desconocido", 404)
    tokens = {}
    if agent:
        for action in ("refresh", "command", "config", "resend"):
            tokens[action] = admin_repo.issue_operation(
                conn, request.state.session.digest, agent["agent_id"], action, now
            )
    tokens["policy"] = admin_repo.issue_operation(
        conn, request.state.session.digest, None, "policy", now
    )
    info = {
        "page": page,
        "agents": agents,
        "agent": agent,
        "tokens": tokens,
        "policy": asdict(public_repo.monitoring_policy(conn)),
        "hours": hours,
        "resource": resource,
        "overall": worst(a["state"] for a in agents),
        "boot": admin_repo.boot_info(conn),
        "outcome": request.query_params.get("outcome"),
    }
    if agent:
        agent_id = agent["agent_id"]
        info["metrics"] = metrics(
            private_repo.samples(conn, agent_id, resource, now - timedelta(hours=hours)), now, hours
        )
        info["availability"] = {
            key: metrics(
                private_repo.samples(conn, agent_id, "@" + key, now - timedelta(hours=hours)),
                now,
                hours,
            )
            for key in ("ip", "dns")
        }
        info["counters"] = private_repo.counters(conn, agent_id, now - timedelta(hours=hours))
        info["observations"] = private_repo.observations(conn, agent_id, resource)
        info["desired"] = control_repo.latest_desired(conn, agent_id)
        info["observed_config"] = control_repo.get_observed(conn, agent_id)
        info["diagnostic_records"] = json.loads(agent["snapshot"].get("diagnostics") or "{}").get(
            "records", []
        )
    if page == "history":
        params = request.query_params
        limit = int(params.get("limit", "50"))
        if not 1 <= limit <= 100 or len(params.get("cursor", "")) > 2048:
            raise ValueError
        info["history"] = admin_repo.history(
            conn,
            agent=selected,
            record=resource,
            event_type=params.get("type"),
            since=params.get("since"),
            until=params.get("until"),
            cursor=params.get("cursor"),
            limit=limit,
        )
        if info["history"]["next_cursor"]:
            info["next_history"] = urlencode(
                {**dict(params), "cursor": info["history"]["next_cursor"]}
            )
        info["audit"] = admin_repo.audit_entries(conn)
    info["missing_configs"] = private_repo.max_check_interval(conn)[1]
    return info


@router.get("/", response_model=None)
@router.get("/records", response_model=None)
@router.get("/history", response_model=None)
@router.get("/diagnostics", response_model=None)
@router.get("/config", response_model=None)
async def page(request: Request):
    name = request.url.path.rsplit("/", 1)[-1] or "dashboard"
    try:
        return render(request, "page", **context(request, name))
    except (ValueError, TypeError) as exc:
        return render(
            request,
            "message",
            status=getattr(exc, "status", 400),
            message=str(exc)
            if isinstance(exc, admin_actions.ActionError)
            else "Filtros no válidos",
        )


@router.get("/api/v1/status", response_model=None)
async def status_api(request: Request):
    return JSONResponse({"agents": summaries(request)})


@router.get("/api/v1/history", response_model=None)
async def history_api(request: Request):
    try:
        return JSONResponse(context(request, "history")["history"])
    except (ValueError, TypeError):
        return error(400, "Filtros no válidos")


@router.get("/commands/{command_id}", response_model=None)
async def command_page(command_id: str, request: Request):
    command = control_repo.get_command(request.app.state.db, command_id)
    if command is None:
        return render(request, "message", status=404, message="Comando desconocido")
    token = admin_repo.issue_operation(
        request.app.state.db,
        request.state.session.digest,
        command.agent_id,
        "command-refresh",
        utc_now(),
    )
    return render(
        request,
        "command",
        command=command,
        operation_id=token,
        details=control.command_details(command),
    )


def config_form(data):
    """Transforma controles HTML en el contrato; no valida reglas DDNS."""
    count = int(data.get("record_count", "0"))
    if not 0 <= count <= 1000:
        raise ValueError
    records = []
    for index in range(count):
        name = data.get(f"record_{index}_name", "").strip()
        if not name:
            continue
        record = {
            "name": name,
            "mode": data.get(f"record_{index}_mode"),
            "enabled": data.get(f"record_{index}_enabled") == "on",
        }
        provider = data.get(f"record_{index}_provider")
        if provider:
            record["provider"] = provider
        records.append(record)
    return {
        "check_interval_seconds": int(data["check_interval_seconds"]),
        "ip_sources": {"urls": data["ip_sources"].splitlines()},
        "dns": {"resolvers": data["resolvers"].splitlines()},
        "retention": {
            "samples_days": int(data["samples_days"]),
            "events_days": int(data["events_days"]),
        },
        "update": {
            "verify_retry_seconds": int(data["verify_retry_seconds"]),
            "verify_timeout_seconds": int(data["verify_timeout_seconds"]),
        },
        "records": records,
    }


@router.post("/actions/{action}", response_model=None)
@router.post("/api/v1/{action}", response_model=None)
async def action(action: str, request: Request):
    data = dict(request.state.form)
    data.pop("csrf", None)
    try:
        if action == "config" and "content" not in data:
            content = config_form(data)
            data = {
                key: data[key]
                for key in (
                    "agent_id",
                    "operation_id",
                    "active_version",
                    "active_hash",
                    "desired_version",
                )
            }
            data["content"] = content
        result = await admin_actions.execute(
            request.app.state.db,
            request.state.session,
            action,
            data,
            request.app.state.settings,
            request.app.state.agent_transport,
        )
    except (ValueError, TypeError, KeyError, ValidationError) as exc:
        status = exc.status if isinstance(exc, admin_actions.ActionError) else 400
        message = str(exc) if isinstance(exc, admin_actions.ActionError) else "Formulario no válido"
        if "/api/" in request.url.path:
            return error(status, message)
        return render(request, "message", status=status, message=message)
    if "/api/" in request.url.path:
        return JSONResponse(result)
    if action in ("command", "command-refresh"):
        return RedirectResponse("/admin/commands/" + result["reference"], status_code=303)
    destination = "/admin/diagnostics" if action == "refresh" else "/admin/config"
    outcome = "pending" if result.get("result") in ("uncertain", "rejected") else "registered"
    return RedirectResponse(
        destination + "?" + urlencode({"agent": data.get("agent_id", ""), "outcome": outcome}),
        status_code=303,
    )
