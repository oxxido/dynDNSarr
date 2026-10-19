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
