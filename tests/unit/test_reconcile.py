from datetime import UTC, datetime, timedelta

import pytest

from dyndnsarr_agent.db.ddns_repo import RecordState, iso
from dyndnsarr_agent.ddns.reconcile import (
    after_update,
    backoff_seconds,
    classify_answers,
    decide,
    starts_failure_streak,
)
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.providers.base import UpdateResult

A, B = "1.2.3.4", "5.6.7.8"
NAME = "home.example.com"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
TIMEOUT = 900


def answers(*per_resolver):
    """Cada argumento es lo que responde un resolver: una tupla de IP o un código de error."""
    return [
        ResolverAnswer(f"r{i}", (), x) if isinstance(x, str) else ResolverAnswer(f"r{i}", x, None)
        for i, x in enumerate(per_resolver)
    ]


def state(verification="unknown", **kw):
    return RecordState(NAME, verification, kw.pop("since", "2026-10-17T11:00:00Z"), **kw)


def accepted(ip=B, ago=60, **kw):
    return {"accepted_ip": ip, "accepted_at": iso(NOW - timedelta(seconds=ago)), **kw}


def run(public_ip, resolvers, st, manual=False, now=NOW):
    return decide(public_ip, answers(*resolvers), st, now, TIMEOUT, manual)


# --- clasificación de las respuestas ---


@pytest.mark.parametrize(
    "resolvers,kind",
    [
        ([(B,), (B,)], "target"),
        ([(B,), "timeout"], "target"),  # solo cuentan los que responden
        ([(A,), (A,)], "stale"),
        ([(A, B), (A, B)], "stale"),  # la IP junto a otra no es el objetivo
        ([(A,), (B,)], "split"),
        (["nxdomain", "timeout"], "none"),
        ([(), "no_answer"], "none"),
    ],
)
def test_classify_answers(resolvers, kind):
    assert classify_answers(B, answers(*resolvers)) == kind


# --- una fila por línea de la tabla de `decide` ---


def test_uncertain_ip_changes_nothing_and_does_not_call():
    st = state("mismatch", **accepted())
    decision = decide(None, answers((A,)), st, NOW, TIMEOUT)
    assert (decision.call, decision.state) == (False, st)


def test_target_verifies_and_clears_acceptance_and_failures():
    st = state(
        "pending_verification",
        **accepted(),
        failures=2,
        failure_ip=B,
        next_attempt_at="2026-10-17T12:30:00Z",
        last_error_code="timeout",
    )
    d = run(B, [(B,), (B,)], st)
    assert d.call is False
    assert d.state == RecordState(NAME, "verified", iso(NOW))
    assert d.verification_timed_out_after is None


def test_verified_since_is_kept_while_the_state_does_not_change():
    st = state("verified", since="2026-10-01T00:00:00Z")
    assert run(B, [(B,)], st).state.since == "2026-10-01T00:00:00Z"


@pytest.mark.parametrize(
    "previous,expected",
    [
        ("pending_verification", "pending_verification"),
        ("mismatch", "unknown"),
        ("verified", "unknown"),
        ("unknown", "unknown"),
    ],
)
def test_no_answers_keeps_pending_otherwise_unknown(previous, expected):
    d = run(B, ["timeout", "nxdomain"], state(previous))
    assert (d.call, d.state.verification) == (False, expected)


def test_split_is_pending_when_this_ip_was_accepted():
    d = run(B, [(A,), (B,)], state("pending_verification", **accepted(B)))
    assert (d.call, d.state.verification) == (False, "pending_verification")


def test_split_is_mismatch_when_this_ip_was_not_accepted():
    for st in (state("unknown"), state("pending_verification", **accepted(A))):
        d = run(B, [(A,), (B,)], st)
        assert (d.call, d.state.verification) == (False, "mismatch")


def test_stale_with_accepted_ip_within_the_deadline_waits_without_calling():
    d = run(B, [(A,), (A,)], state("pending_verification", **accepted(B, ago=TIMEOUT - 1)))
    assert (d.call, d.state.verification) == (False, "pending_verification")
    assert d.state.accepted_ip == B
    assert d.verification_timed_out_after is None


def test_stale_with_accepted_ip_after_the_deadline_reports_it_once_and_allows_a_call():
    d = run(B, [(A,), (A,)], state("pending_verification", **accepted(B, ago=TIMEOUT)))
    assert d.call is True
    assert d.verification_timed_out_after == TIMEOUT
    assert (d.state.verification, d.state.accepted_ip, d.state.accepted_at) == (
        "mismatch",
        None,
        None,
    )


def test_expired_deadline_without_a_call_still_discards_the_acceptance():
    st = state(
        "pending_verification",
        **accepted(B, ago=TIMEOUT + 5),
        failures=1,
        failure_ip=B,
        next_attempt_at="2026-10-17T12:05:00Z",
        last_error_code="timeout",
    )
    d = run(B, [(A,), (A,)], st)
    assert (d.call, d.state.accepted_ip, d.verification_timed_out_after) == (
        False,
        None,
        TIMEOUT + 5,
    )


def test_stale_during_backoff_does_not_call():
    st = state(
        "mismatch", failures=1, failure_ip=B, next_attempt_at=iso(NOW + timedelta(seconds=1))
    )
    d = run(B, [(A,)], st)
    assert (d.call, d.state.verification) == (False, "mismatch")


def test_stale_after_backoff_calls_again():
    st = state("mismatch", failures=1, failure_ip=B, next_attempt_at=iso(NOW))
    assert run(B, [(A,)], st).call is True


def test_stale_after_a_definitive_error_for_this_ip_never_calls_by_itself():
    st = state("mismatch", failures=1, failure_ip=B, last_error_code="auth_failed")
    late = NOW + timedelta(days=30)
    d = run(B, [(A,)], st, now=late)
    assert (d.call, d.state.verification) == (False, "mismatch")


def test_a_failure_for_another_ip_does_not_block_a_new_ip():
    st = state(
        "mismatch", failures=4, failure_ip=A, next_attempt_at=None, last_error_code="auth_failed"
    )
    assert run(B, [(A,)], st).call is True
    waiting = state(
        "mismatch", failures=4, failure_ip=A, next_attempt_at=iso(NOW + timedelta(hours=1))
    )
    assert run(B, [(A,)], waiting).call is True


def test_stale_with_nothing_else_calls():
    d = run(B, [(A,), (A,)], state("verified"))
    assert d.call is True
    assert d.state.verification == "mismatch"


# --- `manual` ---


def test_manual_ignores_backoff_and_definitive_errors():
    waiting = state(
        "mismatch", failures=2, failure_ip=B, next_attempt_at=iso(NOW + timedelta(hours=1))
    )
    blocked = state("mismatch", failures=1, failure_ip=B, last_error_code="auth_failed")
    assert run(B, [(A,)], waiting, manual=True).call is True
    assert run(B, [(A,)], blocked, manual=True).call is True


def test_manual_does_not_ignore_the_accepted_ip_within_the_deadline():
    st = state("pending_verification", **accepted(B))
    assert run(B, [(A,)], st, manual=True).call is False


def test_manual_does_not_call_when_the_ip_is_uncertain_or_dns_is_right():
    assert decide(None, answers((A,)), state(), NOW, TIMEOUT, manual=True).call is False
    assert run(B, [(B,)], state("verified"), manual=True).call is False


# --- backoff y resultado de la llamada ---


def test_backoff_doubles_from_60_up_to_3600():
    assert [backoff_seconds(n) for n in range(1, 9)] == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert backoff_seconds(10_000) == 3600


def test_successive_failures_follow_the_backoff_and_persist_in_the_state():
    st = decide(B, answers((A,)), state("verified"), NOW, TIMEOUT).state
    waits = []
    for _ in range(4):
        st = after_update(st, B, UpdateResult(False, error_code="http_error", retryable=True), NOW)
        waits.append(
            (datetime.fromisoformat(st.next_attempt_at.replace("Z", "+00:00")) - NOW).seconds
        )
    assert waits == [60, 120, 240, 480]
    assert (st.failures, st.failure_ip, st.last_error_code) == (4, B, "http_error")
    assert st.verification == "mismatch"


def test_failures_restart_when_the_ip_changes():
    st = state("mismatch", failures=5, failure_ip=A, next_attempt_at=iso(NOW))
    after = after_update(st, B, UpdateResult(False, error_code="timeout", retryable=True), NOW)
    assert (after.failures, after.failure_ip) == (1, B)
    assert after.next_attempt_at == iso(NOW + timedelta(seconds=60))


def test_definitive_failure_has_no_next_attempt():
    st = after_update(
        state("mismatch"), B, UpdateResult(False, error_code="auth_failed", retryable=False), NOW
    )
    assert (st.failures, st.failure_ip, st.next_attempt_at) == (1, B, None)


def test_accepted_update_goes_pending_and_clears_failures():
    st = state("mismatch", failures=3, failure_ip=B, next_attempt_at=iso(NOW), last_error_code="x")
    after = after_update(st, B, UpdateResult(True, changed=True), NOW)
    assert after == RecordState(
        NAME, "pending_verification", iso(NOW), accepted_ip=B, accepted_at=iso(NOW)
    )


def test_failure_events_only_open_a_streak_or_change_the_code():
    fail = UpdateResult(False, error_code="timeout", retryable=True)
    other = UpdateResult(False, error_code="http_error", retryable=True)
    clean = state("mismatch")
    streak = after_update(clean, B, fail, NOW)
    assert starts_failure_streak(clean, B, fail) is True
    assert starts_failure_streak(streak, B, fail) is False
    assert starts_failure_streak(streak, B, other) is True
    assert starts_failure_streak(streak, A, fail) is True
