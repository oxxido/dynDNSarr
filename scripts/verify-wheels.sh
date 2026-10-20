#!/usr/bin/env bash
# Construye los wheels, los instala en un venv limpio, arranca Agent y Hub instalados para
# comprobar /healthz y ejecuta `dyndnsarr-agent status` sobre la BD del Agent. Detecta, p. ej.,
# migraciones .sql que no viajan dentro del paquete.
# Uso: scripts/verify-wheels.sh   (PYTHON=python3.11 para elegir intérprete)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
python="${PYTHON:-python3}"
work="$(mktemp -d)"
pid=""
cleanup() {
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    rm -rf "$work"
}
trap cleanup EXIT
cd "$work"

# Se construye desde una copia limpia: un build/ viejo en el árbol fuente podría ocultar archivos
# que el wheel no incluye.
mkdir "$work/src"
tar -C "$root" --exclude=build --exclude='*.egg-info' --exclude=__pycache__ -cf - packages apps |
    tar -C "$work/src" -xf -
"$python" -m pip wheel --quiet --no-deps -w "$work/wheels" \
    "$work/src/packages/shared" "$work/src/apps/agent" "$work/src/apps/hub"
"$python" -m venv "$work/venv"
"$work/venv/bin/pip" install --quiet "$work"/wheels/*.whl

check() {
    local name="$1" prefix="$2" port
    port="$("$work/venv/bin/python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
    env "${prefix}_PORT=$port" "${prefix}_DATA_DIR=$work/data-$name" \
        "$work/venv/bin/dyndnsarr-$name" 2>"$work/$name.log" &
    pid=$!
    for _ in $(seq 1 50); do
        if "$work/venv/bin/python" - "$port" "$name" <<'PY' 2>/dev/null; then
import json, sys, urllib.request
body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=1))
sys.exit(0 if body["status"] == "ok" and body["component"] == sys.argv[2] else 1)
PY
            echo "OK: $name instalado desde wheel responde en /healthz"
            kill "$pid"; wait "$pid" 2>/dev/null || true; pid=""
            return 0
        fi
        sleep 0.2
    done
    echo "FALLO: $name no respondió en /healthz" >&2
    cat "$work/$name.log" >&2
    return 1
}

check agent DYNDNSARR_AGENT
check hub DYNDNSARR_HUB

# `status` falla si las migraciones (0002, 0003 y 0005: motor DDNS y control del Hub) no viajaron
# en el wheel: lee las columnas de la 0003 y la tabla `configuration_versions` de la 0005.
DYNDNSARR_AGENT_DATA_DIR="$work/data-agent" "$work/venv/bin/dyndnsarr-agent" status >/dev/null
echo "OK: agent instalado desde wheel ejecuta 'dyndnsarr-agent status'"

# Los paquetes `providers`, `outbox` (Agent) y `auth` (Hub) deben viajar en el wheel (se declaran
# a mano en `packages` de los pyproject).
"$work/venv/bin/python" -c 'import dyndnsarr_agent.providers.dondominio, dyndnsarr_agent.providers.registry'
echo "OK: agent instalado desde wheel importa dyndnsarr_agent.providers"
"$work/venv/bin/python" -c 'import dyndnsarr_agent.outbox.client, dyndnsarr_agent.outbox.loop, dyndnsarr_hub.auth.tokens, dyndnsarr_hub.api.events'
echo "OK: agent y hub instalados desde wheel importan outbox, auth y api.events"

# `commands` (Agent) y `services` (Hub) son paquetes del slice 3b, declarados a mano.
"$work/venv/bin/python" -c 'import dyndnsarr_agent.commands.executor, dyndnsarr_agent.api.internal, dyndnsarr_agent.ddns.apply_config, dyndnsarr_hub.services.agent_client, dyndnsarr_hub.services.events_handlers'
echo "OK: agent y hub instalados desde wheel importan commands y services"

# `agent-list` falla si las migraciones 0002 y 0003 del Hub no viajaron en el wheel (lee `api_url`).
DYNDNSARR_HUB_DATA_DIR="$work/data-hub" "$work/venv/bin/dyndnsarr-hub" agent-list >/dev/null
echo "OK: hub instalado desde wheel ejecuta 'dyndnsarr-hub agent-list'"

# Landing pública (slice 4): la plantilla y los estáticos deben viajar en el wheel del Hub (se
# declaran a mano en `package-data`). `/` responde con la plantilla; los PNG de la mascota, si el
# repositorio ya los trae, deben servirse con 200 `image/png`.
port="$("$work/venv/bin/python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
DYNDNSARR_HUB_PORT="$port" DYNDNSARR_HUB_DATA_DIR="$work/data-hub" \
    "$work/venv/bin/dyndnsarr-hub" 2>"$work/hub-public.log" &
pid=$!
for _ in $(seq 1 50); do
    "$work/venv/bin/python" - "$port" <<'PY' 2>/dev/null && break || sleep 0.2
import sys, urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=1)
PY
done
pngs=""
for name in dyno-green dyno-orange dyno-red; do
    [ -f "$root/apps/hub/dyndnsarr_hub/static/assets/$name.png" ] && pngs="$pngs $name.png"
done
"$work/venv/bin/python" - "$port" $pngs <<'PY'
import sys, urllib.request
base = f"http://127.0.0.1:{sys.argv[1]}"
page = urllib.request.urlopen(f"{base}/", timeout=3)
html = page.read().decode()
assert page.status == 200 and "Estado del servicio" in html and "Desconocido" in html, "plantilla"
assert "nonce-" in page.headers["Content-Security-Policy"]
for name in sys.argv[2:]:
    png = urllib.request.urlopen(f"{base}/static/assets/{name}", timeout=3)
    assert png.status == 200 and png.headers["Content-Type"] == "image/png", name
print("OK: hub instalado desde wheel sirve / con plantilla" + (f" y {len(sys.argv) - 2} PNG" if len(sys.argv) > 2 else ""))
if len(sys.argv) <= 2:
    print("AVISO: el repositorio aún no trae los PNG de la mascota (slice 4, paso 9); no se comprobaron", file=sys.stderr)
PY
kill "$pid"; wait "$pid" 2>/dev/null || true; pid=""

# Slice5: autenticación, migraciones y todas las plantillas privadas desde el wheel limpio.
"$work/venv/bin/python" - "$work/private-wheel" <<'PYTEST'
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from dyndnsarr_hub.auth import sessions
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.main import create_app
from dyndnsarr_hub.clock import utc_now
settings = Settings(data_dir=Path(sys.argv[1]), admin_origin="https://hub.test")
app = create_app(settings)
with TestClient(app, base_url="https://hub.test", follow_redirects=False) as client:
    client.portal.call(lambda: sessions.set_password(app.state.db, "wheel-admin",
        "synthetic wheel password", utc_now(), create=True))
    login = client.get("/admin/login")
    assert login.status_code == 200 and 'name="password"' in login.text
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', login.text)[1]
    assert client.post("/admin/login", data={"username": "wheel-admin",
        "password": "synthetic wheel password", "csrf": csrf},
        headers={"Origin": "https://hub.test"}).status_code == 303
    for path in ("/admin/", "/admin/records", "/admin/history", "/admin/diagnostics", "/admin/config"):
        assert client.get(path).status_code == 200, path
    assert client.get("/openapi.json").status_code == 404
print("OK: hub instalado desde wheel autentica y sirve todas las vistas privadas")
PYTEST
