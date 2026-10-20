"""Textos y filas de la página pública a partir del `PublicStatusDTO` (sin HTTP ni SQL).

Los textos son los de la maqueta aprobada. Todo lo que se muestra sale del DTO: la plantilla no
calcula nada. Las horas se muestran en UTC.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from dyndnsarr_hub.clock import parse_instant
from dyndnsarr_hub.public.dto import BUCKET_SECONDS, PublicStatusDTO, State

_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

# Clase de estado de la maqueta y mascota por estado (`unknown` usa la verde, en gris).
STATE_CLASS = {"healthy": "green", "degraded": "orange", "critical": "red", "unknown": "unknown"}
MASCOT = {
    "healthy": "dyno-green.png",
    "degraded": "dyno-orange.png",
    "critical": "dyno-red.png",
    "unknown": "dyno-green.png",
}
# (título, descripción del hero, descripción de la tarjeta, icono)
_STATE_TEXT = {
    "healthy": (
        "Operativo",
        "DynDNSarr está funcionando correctamente. Tu DNS se mantiene actualizado y conectado.",
        "Todos los sistemas funcionando correctamente.",
        "✓",
    ),
    "degraded": (
        "Degradado",
        "Se ha detectado una incidencia. La conectividad puede estar parcialmente afectada.",
        "Algunas comprobaciones requieren atención.",
        "!",
    ),
    "critical": (
        "Sin conexión",
        "El servicio presenta un fallo importante. Estamos mostrando el último estado conocido.",
        "Se ha detectado un problema crítico.",
        "×",
    ),
    "unknown": (
        "Desconocido",
        "No hay datos suficientes para confirmar el estado actual.",
        "A la espera de nuevas comprobaciones.",
        "?",
    ),
}
_DNS_TEXT = {
    "ok": ("Conectado", "Resolución y conectividad correctas."),
    "attention": ("Con incidencias", "Hay verificaciones pendientes."),
    "unknown": ("No verificado", "Sin información reciente."),
}
_CHECK_LABEL = {
    "dns_resolution": "Resolución DNS",
    "ipv4": "Conectividad IPv4",
    "ipv6": "Conectividad IPv6",
    "service": "Respuesta del servicio",
}
# estado → (símbolo, texto, clase)
_CHECK_STATE = {
    "ok": ("✓", "Correcto", ""),
    "attention": ("!", "Atención", "warn"),
    "unavailable": ("–", "No disponible", "muted"),
    "unknown": ("?", "Desconocido", "muted"),
}
_BUCKET_WORD = {
    "healthy": "operativo",
    "degraded": "degradado",
    "critical": "sin conexión",
    "unknown": "sin datos",
}
_BAR_CLASS = {"healthy": "", "degraded": "warn", "critical": "bad", "unknown": "unknown"}


@dataclass(frozen=True)
class Row:
    symbol: str
    label: str
    when: str
    badge: str
    tone: str  # "", "warn" o "muted": clase extra del símbolo y de la insignia


@dataclass(frozen=True)
class Bar:
    css: str
    title: str


@dataclass(frozen=True)
class PageView:
    state_class: str
    mascot: str
    title: str
    description: str
    card_description: str
    icon: str
    dns_title: str
    dns_description: str
    last_check: str
    last_check_detail: str
    last_check_short: str
    renewals: list[Row]
    checks: list[Row]
    bars: list[Bar]
    day_labels: list[str]
    coverage_percent: int
    generated: str


def _date(moment: datetime) -> str:
    return f"{moment.day} {_MONTHS[moment.month - 1]}"


def _date_time(moment: datetime) -> str:
    return f"{_date(moment)}, {moment:%H:%M}"


def relative(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return "Hace unos segundos"
    for unit, size in (("día", 86400), ("hora", 3600), ("minuto", 60)):
        if seconds >= size:
            n = seconds // size
            return f"Hace {n} {unit}{'s' if n != 1 else ''}"
    raise AssertionError("inalcanzable")


def _renewal_row(renewal) -> Row:
    label = f"IP terminada en {renewal.ip_suffix}"
    if renewal.record is not None:
        label = f"{renewal.record} · {label}"
    pending = renewal.verification == "pending"
    return Row(
        "…" if pending else "✓",
        label,
        _date_time(parse_instant(renewal.occurred_at)),
        "Pendiente de verificación" if pending else "Correcta",
        "warn" if pending else "",
    )


def build_view(dto: PublicStatusDTO) -> PageView:
    state: State = dto.state
    title, description, card_description, icon = _STATE_TEXT[state]
    dns_title, dns_description = _DNS_TEXT[dto.dns]
    generated = parse_instant(dto.generated_at)
    if dto.last_successful_check_at is None:
        last, detail, short = (
            "Sin comprobaciones",
            "Aún no hay una comprobación satisfactoria.",
            "—",
        )
    else:
        moment = parse_instant(dto.last_successful_check_at)
        last = relative(generated - moment)
        detail = f"{_date_time(moment)} UTC"
        short = last.lower()
    start = parse_instant(dto.history.from_)
    bars = []
    for i, bucket in enumerate(dto.history.buckets):
        begin = start + timedelta(seconds=BUCKET_SECONDS * i)
        end = begin + timedelta(seconds=BUCKET_SECONDS)
        text = f"{_date_time(begin)}–{end:%H:%M} UTC: {_BUCKET_WORD[bucket]}"
        bars.append(
            Bar(_BAR_CLASS[bucket], f"Intervalo {i + 1} de {len(dto.history.buckets)}, {text}")
        )
    return PageView(
        state_class=STATE_CLASS[state],
        mascot=MASCOT[state],
        title=title,
        description=description,
        card_description=card_description,
        icon=icon,
        dns_title=dns_title,
        dns_description=dns_description,
        last_check=last,
        last_check_detail=detail,
        last_check_short=short,
        renewals=[_renewal_row(r) for r in dto.renewals],
        checks=[
            Row(
                _CHECK_STATE[c.state][0],
                _CHECK_LABEL[c.id],
                "",
                _CHECK_STATE[c.state][1],
                _CHECK_STATE[c.state][2],
            )
            for c in dto.checks
        ],
        bars=bars,
        day_labels=[_date(start + timedelta(days=d)) for d in range(7)],
        coverage_percent=round(dto.history.coverage * 100),
        generated=f"{_date_time(generated)} UTC",
    )
