import asyncio
from dataclasses import dataclass
from typing import Protocol

import dns.asyncresolver
import dns.exception
import dns.resolver

from dyndnsarr_agent.ddns.config import Resolver


@dataclass(frozen=True)
class ResolverAnswer:
    """Lo que un resolver externo responde para un nombre: registros A o un código de error."""

    resolver: str
    ips: tuple[str, ...]
    error_code: str | None


class DnsObserver(Protocol):
    async def lookup(self, name: str) -> list[ResolverAnswer]:
        """Pregunta `name` (registro A) a cada resolver configurado, en su orden."""
        ...


class DnspythonObserver:
    def __init__(self, resolvers: list[Resolver], timeout: float = 5.0) -> None:
        self._resolvers = resolvers
        self._timeout = timeout

    async def _ask(self, resolver: Resolver, name: str) -> ResolverAnswer:
        # `configure=False`: no se lee /etc/resolv.conf; solo se pregunta a este resolver.
        stub = dns.asyncresolver.Resolver(configure=False)
        stub.nameservers = [resolver.host]
        stub.port = resolver.port
        stub.lifetime = self._timeout
        try:
            answer = await stub.resolve(name, "A", raise_on_no_answer=True)
        except dns.resolver.NXDOMAIN:
            return ResolverAnswer(resolver.label, (), "nxdomain")
        except dns.resolver.NoAnswer:
            return ResolverAnswer(resolver.label, (), "no_answer")
        except dns.resolver.NoNameservers:
            return ResolverAnswer(resolver.label, (), "servfail")
        except dns.exception.Timeout:
            return ResolverAnswer(resolver.label, (), "timeout")
        except dns.exception.DNSException:
            return ResolverAnswer(resolver.label, (), "dns_error")
        return ResolverAnswer(resolver.label, tuple(sorted(r.address for r in answer)), None)

    async def lookup(self, name: str) -> list[ResolverAnswer]:
        return list(await asyncio.gather(*(self._ask(r, name) for r in self._resolvers)))
