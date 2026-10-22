#!/usr/bin/env bash
# Levanta el Hub con Docker Compose, lo reinicia y comprueba que los datos sobreviven
# (boot_count sube en 1) y que la consola del Hub funciona dentro del contenedor. Usa un proyecto
# y puerto propios y borra todo al terminar.
# Requiere Docker. Uso: scripts/verify-compose.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
link_port="${DYNDNSARR_HUB_LINK_PORT:-18443}"
# Como en Dockge (slice 8): el compose.yaml del repo, sin editar, en otro directorio y con la ruta
# del clon y el resto de variables en su `.env`.
stack="$(mktemp -d)"
cp "$root/apps/hub/compose.yaml" "$stack/compose.yaml"
cat >"$stack/.env" <<EOF
DYNDNSARR_SRC=$root
DYNDNSARR_HUB_ADMIN_ORIGIN=https://hub.test
DYNDNSARR_HUB_PORT=${DYNDNSARR_HUB_PORT:-18000}
DYNDNSARR_HUB_LINK_PORT=$link_port
DYNDNSARR_HUB_LINK_NAMES=127.0.0.1,localhost
DYNDNSARR_HUB_PAIRING_CODE=00112233445566778899aabbccddeeff
EOF
compose=(docker compose -p dyndnsarr-verify --project-directory "$stack" -f "$stack/compose.yaml")
trap '"${compose[@]}" down -v --remove-orphans; rm -r -- "$stack"' EXIT

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
# Control del Agent (slice 3b): migración 0003 y dependencia httpx dentro de la imagen.
"${compose[@]}" exec -T hub dyndnsarr-hub agent-set-api verify-agent https://agent.example.lan:8001 >/dev/null
"${compose[@]}" exec -T hub dyndnsarr-hub agent-api-token verify-agent | grep -q '^ddh_'
"${compose[@]}" exec -T hub dyndnsarr-hub agent-list | grep -q 'api: https://agent.example.lan:8001'

# Landing pública (slice 4): la imagen trae la plantilla y responde `/` con 200.
"${compose[@]}" exec -T hub python -c \
    "import urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/'); assert r.status == 200 and b'Estado del servicio' in r.read()"

if [ "$second" -ne $((first + 1)) ]; then
    echo "FALLO: boot_count $first -> $second (se esperaba +1)" >&2
    exit 1
fi
echo "OK: healthcheck correcto y datos conservados tras restart (boot_count $first -> $second)"

# Slice5: dependencia Argon2id y plantilla privada presentes; sin alta web ni OpenAPI público.
"${compose[@]}" exec -T hub python - <<'PY'
import urllib.error
import urllib.request
from argon2 import PasswordHasher
encoded = PasswordHasher().hash("synthetic compose password")
assert encoded.startswith("$argon2id$")
response = urllib.request.urlopen("http://127.0.0.1:8000/admin/login")
assert response.status == 200 and b"aprovisionamiento" in response.read()
try:
    urllib.request.urlopen("http://127.0.0.1:8000/openapi.json")
except urllib.error.HTTPError as exc:
    assert exc.code == 404
else:
    raise AssertionError("OpenAPI expuesto")
print("OK: imagen con Argon2id y login privado")
PY

# Slice8: puerto de enlace HTTPS publicado con la CA propia del Hub (en /data) y token de setup
# del administrador en el log mientras no hay administrador.
python3 - "$link_port" <<'PY'
import json, ssl, sys, urllib.error, urllib.request
port = sys.argv[1]
unverified = ssl.create_default_context()
unverified.check_hostname, unverified.verify_mode = False, ssl.CERT_NONE
request = urllib.request.Request(
    f"https://127.0.0.1:{port}/pair/v1/challenge",
    data=json.dumps({"nonce": "ab" * 32}).encode(),
    headers={"Content-Type": "application/json"},
)
body = json.load(urllib.request.urlopen(request, context=unverified, timeout=5))
verified = ssl.create_default_context(cadata=body["ca_pem"])
try:
    urllib.request.urlopen(f"https://127.0.0.1:{port}/healthz", context=verified, timeout=5)
except urllib.error.HTTPError as exc:
    assert exc.code == 404  # fuera de /api/v1 y /pair/v1 no se sirve nada
else:
    raise AssertionError("el puerto de enlace sirve /healthz")
print("OK: puerto de enlace HTTPS verificable con la CA del desafío")
PY
# Sin tubería: con pipefail, `grep -q` cerraría `logs` antes de tiempo (SIGPIPE).
hub_logs="$("${compose[@]}" logs hub)"
grep -q admin_setup_available <<<"$hub_logs"
echo "OK: compose.yaml sin editar con DYNDNSARR_SRC, puerto de enlace y setup del administrador"
