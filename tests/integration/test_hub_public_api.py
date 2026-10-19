"""`GET /public/v1/status` con el Hub real (slice 4): estados, canarios, desactivación, métodos."""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from dyndnsarr_hub.auth.tokens import hash_token
from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, public_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_hub.public.builder import build_dto
from dyndnsarr_hub.public.state import AgentProjection, RecordProjection
from dyndnsarr_shared.db import connect, migrate, transaction

AGENT = "raspberry-home"
RECORD = "canary-record.example.com"
CANARY_IP = "83.45.12.134"
SECRETS = {
    "ip": CANARY_IP,
    "ip3": "83.45.12",
    "mac": "aa:bb:cc:dd:ee:ff",
    "device": "nas-del-salon",
    "agent_token": "dda_canary-agent-token",
    "api_token": "ddh_canary-api-token",
    "dondominio": "canary-dondominio-key",
}


def now():
    return utc_now().replace(microsecond=0)


def projection(agent=AGENT, records=None, **overrides):
    t = now()
    base = dict(
        agent_id=agent,
        refreshed_at=t,
        ip_status="confirmed",
        public_ip_confirmed_at=t - timedelta(minutes=1),
        last_check_at=t - timedelta(minutes=1),
        records=tuple(
            records
            if records is not None
            else [RecordProjection(RECORD, "verified", t - timedelta(days=1), 0, None, None)]
        ),
    )
    return AgentProjection(**{**base, **overrides})


def make_db(tmp_path, seed=None, agents=(AGENT,)):
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    with transaction(conn):
        for agent in agents:
            agents_repo.create_agent(conn, agent, hash_token(SECRETS["agent_token"] + agent), now())
        # Lo privado que el Hub sí guarda: la vista pública no debe tocarlo.
        if AGENT in agents:
            agents_repo.set_api_token(conn, AGENT, SECRETS["api_token"])
            conn.execute(
                "INSERT INTO events (agent_id, event_id, sequence, type, occurred_at, received_at, "
                "record_name, payload) VALUES (?, 'e1', 1, 'dns_update_succeeded', ?, ?, ?, ?)",
                (
                    AGENT,
                    "2026-10-19T11:00:00Z",
                    "2026-10-19T11:00:00Z",
                    RECORD,
                    json.dumps(
                        {"ip": CANARY_IP, "mac": SECRETS["mac"], "device": SECRETS["device"]}
                    ),
                ),
            )
        if seed:
            seed(conn)
    conn.close()


@contextmanager
def hub(tmp_path, seed=None, agents=(AGENT,), **settings):
    make_db(tmp_path, seed, agents)
    with TestClient(create_app(Settings(data_dir=tmp_path, **settings))) as client:
        yield client


def get(client):
    response = client.get("/public/v1/status")
    assert response.status_code == 200
    return response.json()


def healthy_seed(conn):
    public_repo.save_agent_status(conn, projection())
    public_repo.add_renewal(conn, AGENT, "r1", RECORD, now() - timedelta(hours=2), "34")
    public_repo.add_sample(conn, AGENT, now() - timedelta(hours=1), "healthy")


def test_an_empty_hub_is_unknown_and_valid(tmp_path):
    with hub(tmp_path, agents=()) as client:
        body = get(client)
    assert body["state"] == "unknown" and body["last_successful_check_at"] is None
    assert body["renewals"] == []
    assert body["history"]["buckets"] == ["unknown"] * 84 and body["history"]["coverage"] == 0
    assert body["schema_version"] == 1
    assert [c["id"] for c in body["checks"]] == ["dns_resolution", "ipv4", "ipv6", "service"]
    assert {c["state"] for c in body["checks"]} == {"unknown", "unavailable"}


def test_an_agent_without_projection_is_unknown(tmp_path):
    with hub(tmp_path) as client:
        assert get(client)["state"] == "unknown"


def test_a_healthy_agent(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        body = get(client)
    assert body["state"] == "healthy" and body["dns"] == "ok"
    assert {c["id"]: c["state"] for c in body["checks"]} == {
        "dns_resolution": "ok",
        "ipv4": "ok",
        "ipv6": "unavailable",
        "service": "ok",
    }
    assert body["last_successful_check_at"] is not None
    assert [(r["ip_suffix"], r["verification"], r["record"]) for r in body["renewals"]] == [
        ("34", "pending", None)
    ]
    assert body["history"]["buckets"][-1] == "healthy"
    assert body["history"]["coverage"] == pytest.approx(1 / 84)


def test_degraded_and_critical(tmp_path):
    t = now()

    def degraded(conn):
        public_repo.save_agent_status(conn, projection(ip_status="uncertain"))

    def critical(conn):
        old = RecordProjection(RECORD, "mismatch", t - timedelta(hours=2), 0, None, None)
        public_repo.save_agent_status(conn, projection(records=[old]))

    with hub(tmp_path / "a", degraded) as client:
        body = get(client)
    assert (body["state"], {c["id"]: c["state"] for c in body["checks"]}["ipv4"]) == (
        "degraded",
        "attention",
    )
    with hub(tmp_path / "b", critical) as client:
        body = get(client)
    assert (body["state"], body["dns"]) == ("critical", "attention")


def test_a_stale_projection_is_unknown_but_keeps_the_real_last_check(tmp_path):
    t = now()

    def stale(conn):
        old = t - timedelta(hours=3)
        public_repo.save_agent_status(
            conn, projection(refreshed_at=old, public_ip_confirmed_at=old, last_check_at=old)
        )

    with hub(tmp_path, stale) as client:
        body = get(client)
    assert body["state"] == "unknown" and body["dns"] == "unknown"
    assert body["last_successful_check_at"] == (t - timedelta(hours=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_a_critical_agent_and_an_unknown_one_aggregate_as_critical_then_unknown(tmp_path):
    t = now()

    def seed(conn):
        old = RecordProjection(RECORD, "mismatch", t - timedelta(hours=2), 0, None, None)
        public_repo.save_agent_status(conn, projection(records=[old]))

    with hub(tmp_path / "a", seed, agents=(AGENT, "other")) as client:
        assert get(client)["state"] == "critical"

    def healthy_only(conn):
        public_repo.save_agent_status(conn, projection())

    with hub(tmp_path / "b", healthy_only, agents=(AGENT, "other")) as client:
        assert get(client)["state"] == "unknown"


def test_no_private_value_reaches_the_json_nor_the_headers(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        response = client.get("/public/v1/status")
    text = response.text + str(dict(response.headers))
    for name, secret in SECRETS.items():
        assert secret not in text, name
    assert RECORD not in text


def test_show_domains_adds_only_the_record_name(tmp_path):
    with hub(tmp_path / "a", healthy_seed) as client:
        hidden = get(client)
    with hub(tmp_path / "b", healthy_seed, public_show_domains=True) as client:
        shown = get(client)
    assert shown["renewals"][0]["record"] == RECORD
    shown["renewals"][0]["record"] = None
    for body in (hidden, shown):
        body.pop("generated_at"), body["history"].pop("from")
    assert hidden == shown


def test_response_headers(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        headers = client.get("/public/v1/status").headers
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["content-type"] == "application/json"


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_only_get_is_allowed(tmp_path, method):
    with hub(tmp_path) as client:
        assert getattr(client, method)("/public/v1/status").status_code == 405


def test_disabled_public_routes_are_indistinguishable_from_missing_ones(tmp_path):
    with hub(tmp_path, healthy_seed, public_enabled=False) as client:
        missing = client.get("/no-such-route")
        for path in ("/public/v1/status", "/", "/static/assets/dyno-green.png", "/static/"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.content == missing.content, path
            assert dict(response.headers) == dict(missing.headers), path
        assert client.get("/healthz").status_code == 200


def test_building_the_dto_never_reads_events(tmp_path):
    make_db(tmp_path, healthy_seed)
    conn = connect(tmp_path / "hub.db")
    read_tables = set()

    def authorizer(action, arg1, arg2, db_name, source):
        if action == sqlite3.SQLITE_READ:
            read_tables.add(arg1)
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    build_dto(conn, datetime.now(UTC), Settings(data_dir=tmp_path))
    conn.set_authorizer(None)
    conn.close()
    assert read_tables
    assert "events" not in read_tables and "agent_credentials" not in read_tables


# --- la página `GET /` ---


class Page(HTMLParser):
    """Lo mínimo del DOM que se inspecciona: etiquetas, atributos y texto."""

    def __init__(self, html):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []
        self.text: list[str] = []
        self._stack: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag not in ("img", "meta", "br", "input"):
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self._stack:
            while self._stack and self._stack.pop() != tag:
                pass

    def handle_data(self, data):
        if not {"script", "style"} & set(self._stack):
            self.text.append(data)

    def find(self, tag, **attrs):
        return [
            a for t, a in self.tags if t == tag and all(a.get(k) == v for k, v in attrs.items())
        ]

    @property
    def visible(self):
        return " ".join(" ".join(self.text).split())


def landing(client):
    response = client.get("/")
    assert response.status_code == 200
    return response, Page(response.text)


def seed_state(state):
    t = now()

    def degraded(conn):
        public_repo.save_agent_status(conn, projection(ip_status="uncertain"))

    def critical(conn):
        old = RecordProjection(RECORD, "mismatch", t - timedelta(hours=2), 0, None, None)
        public_repo.save_agent_status(conn, projection(records=[old]))

    return {"healthy": healthy_seed, "degraded": degraded, "critical": critical, "unknown": None}[
        state
    ]


@pytest.mark.parametrize(
    ("state", "title", "klass", "mascot"),
    [
        ("healthy", "Operativo", "state-green", "dyno-green.png"),
        ("degraded", "Degradado", "state-orange", "dyno-orange.png"),
        ("critical", "Sin conexión", "state-red", "dyno-red.png"),
        ("unknown", "Desconocido", "state-unknown", "dyno-green.png"),
    ],
)
def test_each_state_renders_its_class_texts_and_mascot(tmp_path, state, title, klass, mascot):
    with hub(tmp_path, seed_state(state)) as client:
        _, page = landing(client)
    [app] = page.find("div", id="app")
    assert klass in app["class"].split()
    assert page.find("h1", id="heroTitle") and f" {title} " in f" {page.visible} "
    [hero] = page.find("img", id="heroMascot")
    assert hero["src"] == f"/static/assets/{mascot}"
    assert title.lower() in hero["alt"]
    assert ("mascot-unknown" in hero["class"]) == (state == "unknown")
    # El hero y la tarjeta salen de un único valor del estado.
    assert page.visible.count(title) >= 2


def test_unknown_does_not_claim_that_the_connection_is_down(tmp_path):
    with hub(tmp_path) as client:
        _, page = landing(client)
    assert "No hay datos suficientes para confirmar el estado actual." in page.visible
    assert "Estamos mostrando el último estado conocido" not in page.visible
    assert page.visible.count("Sin conexión") == 1  # solo la leyenda del historial


def test_the_mockup_demo_bits_are_gone(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        response, page = landing(client)
    assert [a["id"] for a in page.find("select")] == ["theme"]
    assert "simulad" not in response.text.lower() and "demostraci" not in response.text.lower()
    ipv6 = [t for t in page.visible.split("Conectividad IPv6")[1:2]]
    assert ipv6 and ipv6[0].lstrip().startswith("No disponible")
    assert "IPv6" not in page.visible.replace("Conectividad IPv6", "")


def test_csp_nonce_covers_every_script_and_style_and_changes_per_request(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        first, page = landing(client)
        second, _ = landing(client)
    csp = first.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    assert f"style-src 'nonce-{nonce}'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    scripts, styles = page.find("script"), page.find("style")
    assert len(scripts) == 2 and len(styles) == 1
    assert all(a.get("nonce") == nonce for a in scripts + styles)
    assert nonce not in second.headers["content-security-policy"]
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["referrer-policy"] == "no-referrer"
    assert first.headers["content-type"].startswith("text/html")


def test_the_page_has_no_inline_style_attributes_nor_event_handlers(tmp_path):
    with hub(tmp_path, healthy_seed, public_show_domains=True) as client:
        _, page = landing(client)
    for tag, attrs in page.tags:
        assert "style" not in attrs, tag
        assert not any(name.startswith("on") for name in attrs), tag
        assert "href" not in attrs, tag
        assert attrs.get("src", "/static/").startswith("/static/"), tag


def test_a_record_name_is_escaped_and_only_shown_when_authorised(tmp_path):
    evil = "<script>alert(1)</script>.example.com"

    def seed(conn):
        healthy_seed(conn)
        conn.execute("UPDATE public_renewals SET record_name = ?", (evil,))

    with hub(tmp_path / "a", seed, public_show_domains=True) as client:
        response, page = landing(client)
    assert evil not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;.example.com" in response.text
    assert len(page.find("script")) == 2
    with hub(tmp_path / "b", seed) as client:
        response, _ = landing(client)
    assert "alert(1)" not in response.text


def test_no_private_value_reaches_the_html(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        response, _ = landing(client)
    text = response.text + str(dict(response.headers))
    for name, secret in SECRETS.items():
        assert secret not in text, name
    assert RECORD not in text and "IP terminada en 34" in text


def test_renewal_rows_show_the_suffix_and_their_verification(tmp_path):
    t = now()

    def seed(conn):
        healthy_seed(conn)
        public_repo.add_renewal(conn, AGENT, "r0", RECORD, t - timedelta(days=1), "07")
        public_repo.mark_verified(conn, AGENT, RECORD, "07", t)

    with hub(tmp_path, seed) as client:
        _, page = landing(client)
    text = page.visible
    assert "IP terminada en 34" in text and "Pendiente de verificación" in text
    assert "IP terminada en 07" in text and "Correcta" in text
    assert text.index("IP terminada en 34") < text.index("IP terminada en 07")


def test_an_empty_renewal_list_says_so(tmp_path):
    with hub(tmp_path) as client:
        _, page = landing(client)
    assert "Aún no hay renovaciones registradas." in page.visible
    assert "Sin comprobaciones" in page.visible


def test_the_history_has_84_labelled_bars(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        _, page = landing(client)
    bars = [a for t, a in page.tags if t == "button" and "bar" in a["class"].split()]
    assert len(bars) == 84
    assert all(b["title"] and b["aria-label"] == b["title"] for b in bars)
    assert bars[-1]["class"] == "bar" and "operativo" in bars[-1]["title"]
    assert all("unknown" in b["class"] and "sin datos" in b["title"] for b in bars[:-1])


def test_theme_selector_persists_before_first_paint(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        response, page = landing(client)
    options = re.findall(r'<option value="(\w+)">', response.text)
    assert options == ["system", "light", "dark"]
    head = response.text.split("<style", 1)[0]
    assert "dyndnsarr-theme" in head and "prefers-color-scheme" in head
    assert "localStorage.setItem('dyndnsarr-theme'" in response.text
    assert "prefers-reduced-motion:reduce" in response.text
    assert "color-scheme" in response.text and page.find("meta", name="viewport")


# --- estáticos y aislamiento del Agent ---

ASSETS = Path(__file__).parents[2] / "apps/hub/dyndnsarr_hub/static/assets"
MASCOTS = ("dyno-green.png", "dyno-orange.png", "dyno-red.png")


@pytest.mark.skipif(
    not all((ASSETS / m).exists() for m in MASCOTS),
    reason="faltan los PNG de la mascota (slice 4, paso 9: los aporta el usuario)",
)
@pytest.mark.parametrize("name", MASCOTS)
def test_the_mascot_pngs_are_served(tmp_path, name):
    with hub(tmp_path) as client:
        response = client.get(f"/static/assets/{name}")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "path",
    [
        "/static/%2e%2e/main.py",
        "/static/..%2fmain.py",
        "/static/assets/../../main.py",
        "/static/assets/",
        "/static/",
        "/static/assets/missing.png",
    ],
)
def test_static_does_not_escape_its_directory_nor_list_it(tmp_path, path):
    with hub(tmp_path) as client:
        assert client.get(path).status_code == 404


def test_public_requests_never_call_the_agent(tmp_path):
    calls = []

    def agent(request):
        calls.append(request.url.path)
        raise httpx.ConnectError("sin Agent")

    make_db(tmp_path, healthy_seed)
    conn = connect(tmp_path / "hub.db")
    with transaction(conn):
        agents_repo.set_api_url(conn, AGENT, "https://agent.test")
    conn.close()
    app = create_app(Settings(data_dir=tmp_path, status_refresh_seconds=3600))
    app.state.agent_transport = httpx.MockTransport(agent)
    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        while not calls:  # la primera pasada del refresco, al arrancar
            assert time.monotonic() < deadline
            time.sleep(0.01)
        before = list(calls)
        for path in ("/", "/public/v1/status", "/", "/public/v1/status"):
            assert client.get(path).status_code == 200
        assert calls == before == ["/internal/v1/status"]
