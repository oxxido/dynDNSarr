import asyncio

from dyndnsarr_agent.ddns.config import Resolver
from dyndnsarr_agent.dns.resolver import DnspythonObserver, ResolverAnswer


def lookup(resolvers, name="home.example.com", timeout=1.0):
    observer = DnspythonObserver(resolvers, timeout=timeout)
    return asyncio.run(observer.lookup(name))


def resolver_of(fake):
    return Resolver(host="127.0.0.1", port=fake.port)


def test_returns_the_a_records_per_resolver(fake_dns):
    fake_dns.answers["home.example.com"] = ["1.2.3.4"]
    [answer] = lookup([resolver_of(fake_dns)])
    assert answer == ResolverAnswer(resolver=fake_dns.resolver, ips=("1.2.3.4",), error_code=None)


def test_multiple_records_are_sorted(fake_dns):
    fake_dns.answers["home.example.com"] = ["5.6.7.8", "1.2.3.4"]
    [answer] = lookup([resolver_of(fake_dns)])
    assert answer.ips == ("1.2.3.4", "5.6.7.8")


def test_nxdomain_is_a_safe_code(fake_dns):
    [answer] = lookup([resolver_of(fake_dns)])
    assert (answer.ips, answer.error_code) == ((), "nxdomain")


def test_servfail_is_a_safe_code(fake_dns):
    fake_dns.answers["home.example.com"] = "SERVFAIL"
    [answer] = lookup([resolver_of(fake_dns)])
    assert (answer.ips, answer.error_code) == ((), "servfail")


def test_timeout_is_a_safe_code(fake_dns):
    fake_dns.answers["home.example.com"] = "TIMEOUT"
    [answer] = lookup([resolver_of(fake_dns)], timeout=0.3)
    assert (answer.ips, answer.error_code) == ((), "timeout")


def test_each_resolver_is_asked_separately_and_in_configured_order(fake_dns, make_fake_dns):
    other = make_fake_dns()
    fake_dns.answers["home.example.com"] = ["1.2.3.4"]
    other.answers["home.example.com"] = ["5.6.7.8"]
    answers = lookup([resolver_of(fake_dns), resolver_of(other)])
    assert [(a.resolver, a.ips) for a in answers] == [
        (fake_dns.resolver, ("1.2.3.4",)),
        (other.resolver, ("5.6.7.8",)),
    ]


def test_resolver_label_omits_default_port():
    assert Resolver(host="1.1.1.1").label == "1.1.1.1"
    assert Resolver(host="1.1.1.1", port=5353).label == "1.1.1.1:5353"
