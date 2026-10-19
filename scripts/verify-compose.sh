#!/usr/bin/env bash
# Levanta el Hub con Docker Compose, lo reinicia y comprueba que los datos sobreviven
# (boot_count sube en 1) y que la consola del Hub funciona dentro del contenedor. Usa un proyecto
# y puerto propios y borra todo al terminar.
# Requiere Docker. Uso: scripts/verify-compose.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
export DYNDNSARR_HUB_PORT="${DYNDNSARR_HUB_PORT:-18000}"
compose=(docker compose -p dyndnsarr-verify -f "$root/apps/hub/compose.yaml")
trap '"${compose[@]}" down -v --remove-orphans' EXIT

boot_count() {
    "${compose[@]}" exec -T hub python -c \
        "import sqlite3; print(sqlite3.connect('/data/hub.db').execute(\"SELECT value FROM app_state WHERE key = 'boot_count'\").fetchone()[0])"
}

"${compose[@]}" up -d --build --wait
first="$(boot_count)"
"${compose[@]}" restart hub
# --no-recreate: en la CI `up` recreaba el contenedor tras el restart (un arranque de más:
# boot_count 1 -> 3). Aquí solo se espera a que vuelva a estar healthy.
"${compose[@]}" up -d --wait --no-recreate
second="$(boot_count)"

# La consola del Hub corre dentro del contenedor (usuario no root, /data): alta y listado.
"${compose[@]}" exec -T hub dyndnsarr-hub agent-add verify-agent | grep -q '^dda_'
"${compose[@]}" exec -T hub dyndnsarr-hub agent-list | grep -q '^verify-agent '

if [ "$second" -ne $((first + 1)) ]; then
    echo "FALLO: boot_count $first -> $second (se esperaba +1)" >&2
    exit 1
fi
echo "OK: healthcheck correcto y datos conservados tras restart (boot_count $first -> $second)"
