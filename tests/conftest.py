import http.server
import importlib
import json
import socketserver
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import dns.flags
import dns.message
import dns.rcode
import dns.rrset
import pytest
import trustme
import uvicorn


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


class FakeIpSource:
    """Servidor HTTP en loopback que devuelve `ip` como texto (o 503 si `ip` es None)."""

    def __init__(self, ip: str | None) -> None:
        self.ip = ip
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if fake.ip is None:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = f"{fake.ip}\n".encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def make_fake_ip_source():
    sources = []

    def make(ip: str | None) -> FakeIpSource:
        sources.append(FakeIpSource(ip))
        return sources[-1]

    yield make
    for source in sources:
        source.close()


class FakeDonDominio:
    """Servidor HTTP en loopback que imita a DonDNS (`GET /json/`).

    `calls` guarda los parámetros de cada petición. `mode`: "ok" (acepta), "down" (503) o "auth"
    (`Wrong key`). Al aceptar, si `propagate`, cambia lo que responde `dns` (el DNS falso).
    """

    def __init__(self, dns: "FakeDns") -> None:
        self.dns = dns
        self.mode = "ok"
        self.propagate = True
        self.calls: list[dict[str, str]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                params = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
                fake.calls.append(params)
                status, body = 200, None
                if fake.mode == "down":
                    status, body = 503, b"unavailable"
                elif fake.mode == "auth":
                    body = json.dumps({"success": False, "messages": ["Wrong key"]}).encode()
                else:
                    if fake.propagate:
                        fake.dns.answers[params["host"]] = [params["ip"]]
                    host = {"name": params["host"], "value": params["ip"], "updated": True}
                    body = json.dumps({"success": True, "messages": [], "host": host}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/json/"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def fake_dondominio(fake_dns):
    server = FakeDonDominio(fake_dns)
    yield server
    server.close()


class TlsHub:
    """Un Hub real (`create_app`) servido por Uvicorn con TLS de una CA de `trustme`.

    `ca_file` es la CA que firma su certificado; `other_ca_file`, una CA que no lo firmó.
    """

    def __init__(self, tmp_path) -> None:
        from dyndnsarr_hub.auth.tokens import generate_token, hash_token
        from dyndnsarr_hub.config import Settings
        from dyndnsarr_hub.db import MIGRATIONS, agents_repo
        from dyndnsarr_hub.main import create_app
        from dyndnsarr_shared.db import connect, migrate, transaction

        self.agent_id = "raspberry-home"
        self.token = generate_token()
        self.db_path = tmp_path / "hub-data" / "hub.db"
        conn = connect(self.db_path)
        migrate(conn, MIGRATIONS)
        with transaction(conn):
            agents_repo.create_agent(conn, self.agent_id, hash_token(self.token), datetime.now(UTC))
        conn.close()

        ca, other = trustme.CA(), trustme.CA()
        cert = ca.issue_cert("127.0.0.1", "localhost")
        self.ca_file = tmp_path / "ca.pem"
        self.other_ca_file = tmp_path / "other-ca.pem"
        key_file, cert_file = tmp_path / "hub-key.pem", tmp_path / "hub-cert.pem"
        ca.cert_pem.write_to_path(self.ca_file)
        other.cert_pem.write_to_path(self.other_ca_file)
        cert.private_key_pem.write_to_path(key_file)
        cert.cert_chain_pems[0].write_to_path(cert_file)

        app = create_app(Settings(data_dir=tmp_path / "hub-data"))
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            ssl_keyfile=str(key_file),
            ssl_certfile=str(cert_file),
            log_config=None,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            assert time.monotonic() < deadline, "el Hub TLS de pruebas no arrancó"
            time.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def stored_event_ids(self) -> list[str]:
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            return [r[0] for r in conn.execute("SELECT event_id FROM events ORDER BY sequence")]
        finally:
            conn.close()

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def tls_hub(tmp_path):
    hub = TlsHub(tmp_path)
    yield hub
    hub.close()


class TlsAgent:
    """Un Agent real (`create_app` con API de control) servido con las opciones de `serve()`
    y TLS de una CA de `trustme`. `other_ca_file` es una CA que no firmó su certificado."""

    def __init__(self, tmp_path) -> None:
        import httpx

        from dyndnsarr_agent.config import Settings
        from dyndnsarr_agent.dns.resolver import ResolverAnswer
        from dyndnsarr_agent.main import create_app, server_options

        self.token = "ddh_tls-test-token-0123456789"
        ca, other = trustme.CA(), trustme.CA()
        cert = ca.issue_cert("127.0.0.1", "localhost")
        self.ca_file = tmp_path / "agent-ca.pem"
        self.other_ca_file = tmp_path / "other-ca.pem"
        key_file, cert_file = tmp_path / "agent-key.pem", tmp_path / "agent-cert.pem"
        ca.cert_pem.write_to_path(self.ca_file)
        other.cert_pem.write_to_path(self.other_ca_file)
        cert.private_key_pem.write_to_path(key_file)
        cert.cert_chain_pems[0].write_to_path(cert_file)

        toml = tmp_path / "agent.toml"
        toml.write_text(
            '[ip_sources]\nurls = ["https://a.example/ip", "https://b.example/ip"]\n'
            '[dns]\nresolvers = ["1.1.1.1"]\n'
            '[[records]]\nname = "home.example.com"\nmode = "monitored"\n'
        )
        self.settings = Settings(
            data_dir=tmp_path / "agent-data",
            config_file=toml,
            api_token=self.token,
            tls_cert_file=cert_file,
            tls_key_file=key_file,
        )

        class Observer:
            async def lookup(self, name):
                return [ResolverAnswer("1.1.1.1", ("1.2.3.4",), None)]

        self.app = create_app(
            self.settings,
            observer=Observer(),
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="1.2.3.4")),
        )
        config = uvicorn.Config(self.app, **{**server_options(self.settings), "port": 0})
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            assert time.monotonic() < deadline, "el Agent TLS de pruebas no arrancó"
            time.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def tls_agent(tmp_path):
    agent = TlsAgent(tmp_path)
    yield agent
    agent.close()
