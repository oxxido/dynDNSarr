import importlib
import socketserver
import threading
from dataclasses import dataclass
from types import ModuleType

import dns.flags
import dns.message
import dns.rcode
import dns.rrset
import pytest


@dataclass(frozen=True)
class Component:
    name: str
    env_prefix: str
    default_port: int
    default_data_dir: str
    db_filename: str

    def module(self, sub: str = "") -> ModuleType:
        return importlib.import_module(f"dyndnsarr_{self.name}{'.' + sub if sub else ''}")

    def settings(self, tmp_path, **overrides):
        return self.module("config").Settings(data_dir=tmp_path, **overrides)

    def app(self, settings):
        return self.module("main").create_app(settings)


COMPONENTS = [
    Component("agent", "DYNDNSARR_AGENT_", 8001, "/var/lib/dyndnsarr", "agent.db"),
    Component("hub", "DYNDNSARR_HUB_", 8000, "/data", "hub.db"),
]


@pytest.fixture(params=COMPONENTS, ids=lambda c: c.name)
def component(request) -> Component:
    return request.param


class FakeDns:
    """Servidor DNS UDP mínimo en loopback para pruebas.

    `answers[nombre]` es una lista de IPv4 (registros A), "NXDOMAIN", "SERVFAIL" o "TIMEOUT"
    (no responde). Un nombre ausente responde NXDOMAIN.
    """

    def __init__(self) -> None:
        self.answers: dict[str, list[str] | str] = {}
        self.queries: list[str] = []
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                data, sock = self.request
                query = dns.message.from_wire(data)
                name = query.question[0].name.to_text().rstrip(".").lower()
                fake.queries.append(name)
                answer = fake.answers.get(name, "NXDOMAIN")
                if answer == "TIMEOUT":
                    return
                response = dns.message.make_response(query)
                if answer == "NXDOMAIN":
                    response.set_rcode(dns.rcode.NXDOMAIN)
                elif answer == "SERVFAIL":
                    response.set_rcode(dns.rcode.SERVFAIL)
                else:
                    response.answer.append(
                        dns.rrset.from_text_list(query.question[0].name, 60, "IN", "A", answer)
                    )
                sock.sendto(response.to_wire(), self.client_address)

        self._server = socketserver.ThreadingUDPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def resolver(self) -> str:
        return f"127.0.0.1:{self.port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def make_fake_dns():
    servers = []

    def make() -> FakeDns:
        servers.append(FakeDns())
        return servers[-1]

    yield make
    for server in servers:
        server.close()


@pytest.fixture
def fake_dns(make_fake_dns) -> FakeDns:
    return make_fake_dns()
