import pytest

from dyndnsarr_agent.ddns.cycle import classify
from dyndnsarr_agent.dns.resolver import ResolverAnswer

IP = "1.2.3.4"


def says(*ips, resolver="r"):
    return ResolverAnswer(resolver, tuple(ips), None)


def fails(code="timeout", resolver="r"):
    return ResolverAnswer(resolver, (), code)


@pytest.mark.parametrize(
    "answers,expected",
    [
        ([says(IP), says(IP)], "verified"),
        ([says(IP), fails()], "verified"),  # solo cuentan los que responden
        ([says(IP), says("5.6.7.8")], "mismatch"),  # discrepan entre sí
        ([says("5.6.7.8"), says("5.6.7.8")], "mismatch"),
        ([says(IP, "5.6.7.8")], "mismatch"),  # el registro no apunta solo a nuestra IP
        ([fails(), fails("nxdomain")], "unknown"),
        ([fails("servfail"), fails("no_answer")], "unknown"),
        ([], "unknown"),
    ],
)
def test_classify(answers, expected):
    assert classify(IP, answers) == expected
