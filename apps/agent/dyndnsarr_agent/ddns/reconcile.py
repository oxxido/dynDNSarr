"""Reglas de actualización de un registro `managed`: sin E/S, solo decisiones."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from dyndnsarr_agent.db.ddns_repo import RecordState, iso, parse_iso
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.providers.base import UpdateResult

BACKOFF_INITIAL_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600


@dataclass(frozen=True)
class Decision:
    call: bool
    # Estado resultante si no hay llamada, o estado previo a ella (se completa con `after_update`).
    state: RecordState
    # Segundos que se esperó la propagación cuando el plazo de verificación venció en este ciclo.
    verification_timed_out_after: int | None = None


def backoff_seconds(failures: int) -> int:
    """Espera tras el fallo número `failures` (≥1): 60 s, ×2 en cada fallo, tope 3600 s."""
    return min(BACKOFF_INITIAL_SECONDS * 2 ** min(failures - 1, 16), BACKOFF_MAX_SECONDS)


def classify_answers(public_ip: str, answers: list[ResolverAnswer]) -> str:
    """Qué dicen los resolvers que responden (con registros A) frente a la IP pública.

    `target`: todos la IP. `stale`: todos coinciden en otro valor. `split`: discrepan entre sí.
    `none`: ninguno responde.
    """
    responding = {a.ips for a in answers if a.error_code is None and a.ips}
    if not responding:
        return "none"
    if len(responding) > 1:
        return "split"
    return "target" if responding == {(public_ip,)} else "stale"


def _moved(state: RecordState, verification: str, now: datetime, **changes) -> RecordState:
    since = state.since if verification == state.verification else iso(now)
    return replace(state, verification=verification, since=since, **changes)


def _is_blocked(state: RecordState, public_ip: str, now: datetime) -> bool:
    """Hay un fallo previo para esta IP y no toca reintentar todavía (espera o error definitivo)."""
    if state.failures == 0 or state.failure_ip != public_ip:
        return False
    return state.next_attempt_at is None or iso(now) < state.next_attempt_at


def decide(
    public_ip: str | None,
    answers: list[ResolverAnswer],
    state: RecordState,
    now: datetime,
    verify_timeout_seconds: int,
    manual: bool = False,
) -> Decision:
    """Llamar o no al proveedor y estado resultante (tabla de decisiones del slice 2b).

    Con `manual` se ignoran la espera de backoff y el bloqueo por error definitivo, pero no la
    aceptación dentro de plazo: una IP ya aceptada no se vuelve a enviar mientras se propaga.
    """
    if public_ip is None:
        return Decision(False, state)
    kind = classify_answers(public_ip, answers)
    accepted = state.accepted_ip == public_ip
    if kind == "target":
        return Decision(
            False,
            _moved(
                state,
                "verified",
                now,
                accepted_ip=None,
                accepted_at=None,
                failures=0,
                failure_ip=None,
                next_attempt_at=None,
                last_error_code=None,
            ),
        )
    if kind == "none":
        pending = state.verification == "pending_verification"
        return Decision(False, _moved(state, "pending_verification" if pending else "unknown", now))
    if kind == "split":
        return Decision(
            False, _moved(state, "pending_verification" if accepted else "mismatch", now)
        )
    timed_out_after = None
    if accepted:
        waited = now - parse_iso(state.accepted_at)
        if waited < timedelta(seconds=verify_timeout_seconds):
            return Decision(False, _moved(state, "pending_verification", now))
        timed_out_after = int(waited.total_seconds())
        state = replace(state, accepted_ip=None, accepted_at=None)
    call = manual or not _is_blocked(state, public_ip, now)
    return Decision(call, _moved(state, "mismatch", now), timed_out_after)


def after_update(state: RecordState, ip: str, result: UpdateResult, now: datetime) -> RecordState:
    """Estado tras la llamada: aceptada -> `pending_verification`; fallo -> espera o bloqueo."""
    if result.accepted:
        return _moved(
            state,
            "pending_verification",
            now,
            accepted_ip=ip,
            accepted_at=iso(now),
            failures=0,
            failure_ip=None,
            next_attempt_at=None,
            last_error_code=None,
        )
    failures = state.failures + 1 if state.failure_ip == ip else 1
    next_attempt_at = (
        iso(now + timedelta(seconds=backoff_seconds(failures))) if result.retryable else None
    )
    return replace(
        state,
        failures=failures,
        failure_ip=ip,
        next_attempt_at=next_attempt_at,
        last_error_code=result.error_code,
    )


def starts_failure_streak(state: RecordState, ip: str, result: UpdateResult) -> bool:
    """El fallo merece un evento: empieza una racha o cambia el código (no cada reintento)."""
    return (
        state.failures == 0 or state.failure_ip != ip or state.last_error_code != result.error_code
    )
