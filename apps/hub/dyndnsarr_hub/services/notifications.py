"""Canales de alertas. SMTP bloqueante en un hilo; red siempre fuera de transacciones."""

import asyncio
import smtplib
from email.message import EmailMessage

import httpx

from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.db import alerts_repo
from dyndnsarr_shared.db import transaction
from dyndnsarr_shared.outbound import request, resolve
from dyndnsarr_shared.tls import build_ssl_context


def smtp_send(config, address, username, password, text, delivery_id):
    context = build_ssl_context(config.ca_file)
    message = EmailMessage()
    message["From"], message["To"] = config.sender, ", ".join(config.recipients)
    message["Subject"] = "DynDNSarr · Aviso operativo"
    message["Message-ID"] = f"<{delivery_id}@dyndnsarr.local>"
    message.set_content(text)
    smtp = (
        smtplib.SMTP(timeout=5) if config.starttls else smtplib.SMTP_SSL(context=context, timeout=5)
    )
    try:
        # Conectar a la IP validada manteniendo el hostname original para TLS/SNI.
        smtp._host = config.host
        smtp.connect(address, config.port)
        if config.starttls:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
    finally:
        smtp.close()


async def send(config, settings, delivery, *, transport=None):
    delivery_id, channel, phase, severity, created_at, rule_id = delivery
    text = f"DynDNSarr [{severity}] {rule_id}: {phase} · {created_at} · ID {delivery_id}"
    if channel == "webhook":
        if config.webhook is None or not settings.webhook_url:
            raise ValueError("channel_disabled")
        async with httpx.AsyncClient(
            verify=build_ssl_context(config.webhook.ca_file), trust_env=False, transport=transport
        ) as http:
            await request(
                http,
                "POST",
                settings.webhook_url.get_secret_value(),
                networks=config.webhook.networks,
                headers={"Idempotency-Key": delivery_id},
                body={"text": text},
                parse_json=False,
            )
    else:
        if config.smtp is None:
            raise ValueError("channel_disabled")
        address = await resolve(config.smtp.host, config.smtp.port, config.smtp.networks)
        await asyncio.to_thread(
            smtp_send,
            config.smtp,
            address,
            settings.smtp_user.get_secret_value() if settings.smtp_user else "",
            settings.smtp_password.get_secret_value() if settings.smtp_password else "",
            text,
            delivery_id,
        )


async def deliver(conn, config, settings, *, clock=utc_now, sender=send):
    now = clock()
    with transaction(conn):
        alerts_repo.evaluate(conn, config, now)
    rows = conn.execute(
        "SELECT id,channel,phase,severity,created_at,rule_id,attempts FROM alert_deliveries "
        "WHERE status='pending' AND next_attempt_at<=? ORDER BY next_attempt_at,id LIMIT "
        "10",
        (now.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()
    for row in rows:
        try:
            await sender(config, settings, row[:6])
            code = None
        except Exception:
            code = "delivery_failed"
        with transaction(conn):
            alerts_repo.delivery_result(conn, row[0], row[6] + 1, code, clock())
