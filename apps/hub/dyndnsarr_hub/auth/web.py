"""Controles HTTP comunes de la superficie privada, antes de leer el cuerpo o el recurso."""

import json
import logging
import secrets
from urllib.parse import parse_qs, urlsplit

from fastapi.responses import JSONResponse, RedirectResponse

from dyndnsarr_hub.auth import sessions
from dyndnsarr_hub.clock import utc_now

logger = logging.getLogger(__name__)


def error(code, message, **headers):
    return JSONResponse({"message_safe": message}, status_code=code, headers=headers)


def denied(path):
    return (
        error(401, "Inicia sesión")
        if path.startswith("/admin/api/")
        else RedirectResponse("/admin/login", status_code=303)
    )


def origin(value):
    parts = urlsplit(value)
    return f"{parts.scheme}://{parts.netloc}"


async def protect(request, call_next):
    path = request.url.path
    if path != "/admin" and not path.startswith("/admin/"):
        return await call_next(request)
    nonce = secrets.token_urlsafe(16)
    request.state.nonce = nonce
    try:
        response = None
        if not path.startswith("/admin/static/"):
            conn = request.app.state.db
            raw = request.cookies.get(sessions.COOKIE, "")
            session = sessions.get(conn, raw, utc_now())
            request.state.session = session
            request.state.csrf = sessions.csrf(raw) if session else ""
            if path != "/admin/login" and (session is None or session.user_id is None):
                response = denied(path)
            elif request.method not in ("GET", "HEAD", "OPTIONS"):
                response = await validate_body(request, session, raw)
        if response is None:
            response = await call_next(request)
    except Exception as exc:
        logger.error("admin_request_error", extra={"error_type": type(exc).__name__})
        response = error(500, "Error interno de administración")
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; img-src 'self'; "
                f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            ),
        }
    )
    return response


async def validate_body(request, session, raw):
    maximum = 4096 if request.url.path == "/admin/login" else 256 * 1024
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            return error(413, "El cuerpo supera el límite")
        chunks.append(chunk)
    try:
        body = b"".join(chunks).decode("utf-8")
        if request.headers.get("content-type", "").split(";")[0] == "application/json":
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError
        else:
            values = parse_qs(body, keep_blank_values=True, max_num_fields=5000)
            if any(len(v) != 1 for v in values.values()):
                raise ValueError
            data = {key: value[0] for key, value in values.items()}
    except (ValueError, UnicodeError):
        return error(400, "Formulario no válido")
    expected_origin = request.app.state.settings.admin_origin
    source = request.headers.get("origin")
    if source is None:
        referer = request.headers.get("referer", "")
        try:
            source = origin(referer)
        except ValueError:
            source = None
    token = request.headers.get("x-csrf-token", data.get("csrf", ""))
    if (
        not session
        or not expected_origin
        or source != expected_origin
        or not isinstance(token, str)
        or not secrets.compare_digest(token.encode(), sessions.csrf(raw).encode())
    ):
        return error(403, "CSRF u origen no válido")
    request.state.form = data
    return None
