#!/usr/bin/env bash
# Ejecuta scripts/install-agent.sh tres veces (con y sin archivos de configuración) en un
# contenedor debian:bookworm (Python 3.11, como la Raspberry; sin systemd) y comprueba que es
# idempotente: tokens, CA, certificado y datos no cambian. Después arranca el Agent con su usuario
# y las dos configuraciones y comprueba `/healthz` por HTTPS con la CA generada, que el ciclo DDNS
# corre sin Hub y que el emparejamiento se reintenta sin secretos en el log.
# Requiere Docker y acceso a Debian y PyPI. Uso: scripts/verify-agent-install.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
docker run --rm -i -v "$root:/src:ro" debian:bookworm bash -euo pipefail <<'IN_CONTAINER'
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv openssl ca-certificates >/dev/null
mkdir /repo
tar -C /src --exclude=./.venv --exclude=./data --exclude='*.egg-info' -cf - . | tar -C /repo -xf -
cd /repo
cat >agent.env <<'EOF'
DYNDNSARR_AGENT_HOST=127.0.0.1
DYNDNSARR_AGENT_PORT=8001
DYNDNSARR_AGENT_CONFIG_FILE=/etc/dyndnsarr/agent.toml
DYNDNSARR_AGENT_PAIRING_CODE=00112233445566778899aabbccddeeff
EOF
sed -e 's/^# \[hub\]/[hub]/' -e 's|^# url = "https://192.168.1.10:8443".*|url = "https://127.0.0.1:9"|' \
    -e 's/^# agent_id = .*/agent_id = "verify-agent"/' \
    apps/agent/config/agent.example.toml >agent.toml

snapshot() { sha256sum /etc/dyndnsarr/generated.env /etc/dyndnsarr/tls/*; }
scripts/install-agent.sh
first="$(snapshot)"
# Segunda vez con los archivos (actualización) y tercera sin ellos (`git pull` y repetir).
scripts/install-agent.sh >/dev/null
rm agent.env agent.toml
scripts/install-agent.sh >/dev/null
[ "$first" = "$(snapshot)" ] || { echo "FALLO: el instalador cambió tokens o certificados" >&2; exit 1; }

perm() { stat -c '%a %U:%G' "$1"; }
[ "$(perm /etc/dyndnsarr/generated.env)" = "600 root:root" ]
[ "$(perm /etc/dyndnsarr/tls/agent-ca.key)" = "600 root:root" ]
[ "$(perm /etc/dyndnsarr/tls/agent.key)" = "640 root:dyndnsarr" ]
[ "$(perm /etc/dyndnsarr/agent.env)" = "640 root:dyndnsarr" ]
grep -q '^DYNDNSARR_AGENT_HUB_TOKEN=dda_' /etc/dyndnsarr/generated.env
grep -q '^DYNDNSARR_AGENT_API_TOKEN=ddh_' /etc/dyndnsarr/generated.env
openssl verify -CAfile /etc/dyndnsarr/tls/agent-ca.pem /etc/dyndnsarr/tls/agent.pem >/dev/null
openssl x509 -in /etc/dyndnsarr/tls/agent.pem -noout -ext subjectAltName | grep -q 'IP Address:127.0.0.1'

# Sin systemd: el directorio de datos lo crearía StateDirectory.
install -d -m 0700 -o dyndnsarr -g dyndnsarr /var/lib/dyndnsarr
set -a
. /etc/dyndnsarr/generated.env
. /etc/dyndnsarr/agent.env
set +a
runuser -u dyndnsarr -- /opt/dyndnsarr/venv/bin/dyndnsarr-agent 2>/tmp/agent.log &
for _ in $(seq 1 30); do
    if python3 -c 'import ssl, urllib.request; urllib.request.urlopen("https://127.0.0.1:8001/healthz", timeout=2, context=ssl.create_default_context(cafile="/etc/dyndnsarr/tls/agent-ca.pem"))' 2>/dev/null; then
        healthy=1
        break
    fi
    sleep 1
done
[ "${healthy:-0}" = 1 ] || { cat /tmp/agent.log >&2; echo "FALLO: /healthz no responde" >&2; exit 1; }
# `pair-status` sale con 1 mientras no hay emparejamiento: se lee su salida, sin tubería.
pair_status() { runuser -u dyndnsarr -- /opt/dyndnsarr/venv/bin/dyndnsarr-agent pair-status || true; }
for _ in $(seq 1 20); do
    grep -q connect_error <<<"$(pair_status)" && break
    sleep 1
done
grep -q connect_error <<<"$(pair_status)"
# El DDNS funciona sin Hub: el primer ciclo termina (las fuentes de IP del ejemplo son reales).
for _ in $(seq 1 30); do
    grep -q cycle_completed /tmp/agent.log && break
    sleep 1
done
grep -q cycle_completed /tmp/agent.log || { cat /tmp/agent.log >&2; echo "FALLO: sin ciclo DDNS" >&2; exit 1; }
if grep -q -e dda_ -e ddh_ -e 00112233445566778899aabbccddeeff /tmp/agent.log; then
    echo "FALLO: secretos en el log" >&2
    exit 1
fi
echo "OK: instalación idempotente, /healthz HTTPS con la CA generada, DDNS sin Hub y emparejamiento en reintento"
IN_CONTAINER
