#!/usr/bin/env bash
# Instala o actualiza el Agent en Debian 12 / Raspberry Pi OS (slice 8, ADR-0013). Idempotente:
# repetirlo actualiza el código y la configuración, pero no cambia tokens, certificados vigentes
# ni datos.
#
# Uso, desde el clon del repositorio:
#   cp apps/agent/config/agent.env.example agent.env      # y rellénalo
#   cp apps/agent/config/agent.example.toml agent.toml    # y rellénalo
#   sudo scripts/install-agent.sh [agent.env] [agent.toml]
#
# Sin los archivos (p. ej. tras `git pull`) se conserva la configuración ya instalada.
# No toca el firewall: imprime la regla recomendada. Guía: docs/operations.md §12.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
env_src="${1:-$root/agent.env}"
toml_src="${2:-$root/agent.toml}"
etc=/etc/dyndnsarr
tls="$etc/tls"
generated="$etc/generated.env"
venv=/opt/dyndnsarr/venv
unit=/etc/systemd/system/dyndnsarr-agent.service

die() {
    echo "ERROR: $*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || die "ejecútalo con sudo"
command -v python3 >/dev/null || die "falta python3: sudo apt install python3 python3-venv"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "hace falta Python 3.11 o superior"
python3 -c 'import ensurepip, venv' 2>/dev/null || die "falta python3-venv: sudo apt install python3-venv"
command -v openssl >/dev/null || die "falta openssl: sudo apt install openssl"

# --- usuario y configuración ---
id dyndnsarr >/dev/null 2>&1 ||
    useradd --system --home-dir /var/lib/dyndnsarr --shell /usr/sbin/nologin dyndnsarr
install -d -m 0750 -o root -g dyndnsarr "$etc"

install_config() {
    if [ -f "$1" ]; then
        install -m 0640 -o root -g dyndnsarr "$1" "$2"
        echo "Configuración instalada: $2"
    elif [ ! -f "$2" ]; then
        die "falta $1: copia el ejemplo de apps/agent/config/ y rellénalo (docs/operations.md §12)"
    fi
}
install_config "$env_src" "$etc/agent.env"
install_config "$toml_src" "$etc/agent.toml"

env_value() { sed -n "s/^$1=//p" "$etc/agent.env" | tail -n 1; }
host="$(env_value DYNDNSARR_AGENT_HOST)"
port="$(env_value DYNDNSARR_AGENT_PORT)"
port="${port:-8001}"
case "$host" in
"" | 0.0.0.0 | ::) die "DYNDNSARR_AGENT_HOST debe ser la IP (o el nombre) LAN de esta máquina" ;;
esac
[ -n "$(env_value DYNDNSARR_AGENT_CONFIG_FILE)" ] ||
    die "agent.env debe definir DYNDNSARR_AGENT_CONFIG_FILE=$etc/agent.toml"

# --- código: desde una copia, para no dejar archivos de root en el clon ---
[ -x "$venv/bin/python" ] || python3 -m venv "$venv"
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
cp -r "$root/packages/shared" "$build/shared"
cp -r "$root/apps/agent" "$build/agent"
"$venv/bin/pip" install --quiet --disable-pip-version-check "$build/shared" "$build/agent"
"$venv/bin/pip" install --quiet --disable-pip-version-check --force-reinstall --no-deps \
    "$build/shared" "$build/agent"
echo "Agent instalado: $("$venv/bin/python" -c 'import dyndnsarr_agent as a; print(a.__version__)')"

# --- tokens: se generan una vez y nunca se sobrescriben ---
if [ ! -f "$generated" ]; then
    token() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
    (
        umask 077
        cat >"$generated.tmp" <<EOF
# Generado por scripts/install-agent.sh: no se sobrescribe ni hay que editarlo.
# Tokens del Agent ante el Hub (dda_) y de su API (ddh_); se entregan al Hub al emparejar.
DYNDNSARR_AGENT_HUB_TOKEN=dda_$(token)
DYNDNSARR_AGENT_API_TOKEN=ddh_$(token)
DYNDNSARR_AGENT_TLS_CA_FILE=$tls/agent-ca.pem
DYNDNSARR_AGENT_TLS_CERT_FILE=$tls/agent.pem
DYNDNSARR_AGENT_TLS_KEY_FILE=$tls/agent.key
EOF
    )
    chown root:root "$generated.tmp"
    mv "$generated.tmp" "$generated"
    echo "Tokens generados: $generated"
fi

# --- TLS de la API: CA propia (solo vale para este Agent) y certificado de 2 años ---
install -d -m 0750 -o root -g dyndnsarr "$tls"
ca="$tls/agent-ca.pem"
ca_key="$tls/agent-ca.key"
cert="$tls/agent.pem"
key="$tls/agent.key"
if [ ! -f "$ca" ] || [ ! -f "$ca_key" ]; then
    (umask 077 && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
        -keyout "$ca_key" -out "$ca" -days 3650 -subj "/CN=DynDNSarr Agent CA $(hostname)" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" 2>/dev/null)
    chmod 0600 "$ca_key"
    chmod 0644 "$ca"
    rm -f "$cert"
    echo "CA del Agent creada: $ca"
fi

if python3 -c 'import ipaddress, sys; ipaddress.ip_address(sys.argv[1])' "$host" 2>/dev/null; then
    san="IP:$host"
    san_text="IP Address:$host"
else
    san="DNS:$host"
    san_text="DNS:$host"
fi
renew=0
if [ ! -f "$cert" ] || [ ! -f "$key" ]; then
    renew=1
elif ! openssl x509 -in "$cert" -noout -checkend $((60 * 86400)) >/dev/null; then
    renew=1
elif ! openssl x509 -in "$cert" -noout -ext subjectAltName | grep -qF "$san_text"; then
    renew=1
fi
if [ "$renew" -eq 1 ]; then
    printf '%s\n' "subjectAltName=$san" "basicConstraints=critical,CA:FALSE" \
        "keyUsage=critical,digitalSignature" "extendedKeyUsage=serverAuth" \
        "subjectKeyIdentifier=hash" "authorityKeyIdentifier=keyid" >"$build/agent.ext"
    (umask 077 && openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
        -keyout "$build/agent.key" -out "$build/agent.csr" -subj "/CN=$host" 2>/dev/null)
    openssl x509 -req -in "$build/agent.csr" -CA "$ca" -CAkey "$ca_key" \
        -set_serial "0x$(openssl rand -hex 16)" -days 730 -sha256 \
        -extfile "$build/agent.ext" -out "$build/agent.pem" 2>/dev/null
    install -m 0640 -o root -g dyndnsarr "$build/agent.key" "$key"
    install -m 0644 -o root -g root "$build/agent.pem" "$cert"
    echo "Certificado de la API emitido para $host (2 años): $cert"
fi

# --- servicio ---
url="https://$host:$port"
case "$host" in *:*) url="https://[$host]:$port" ;; esac
if [ -d /run/systemd/system ]; then
    install -m 0644 "$root/apps/agent/systemd/dyndnsarr-agent.service" "$unit"
    systemctl daemon-reload
    systemctl enable dyndnsarr-agent >/dev/null 2>&1
    systemctl restart dyndnsarr-agent
    ok=0
    for _ in $(seq 1 30); do
        if python3 - "$url/healthz" "$ca" <<'PY' 2>/dev/null; then
import ssl, sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=2, context=ssl.create_default_context(cafile=sys.argv[2]))
PY
            ok=1
            break
        fi
        sleep 1
    done
    [ "$ok" -eq 1 ] || die "el Agent no responde en $url/healthz: journalctl -u dyndnsarr-agent -o cat"
    echo "Agent activo: $url/healthz responde por HTTPS."
else
    echo "Sin systemd: no se instala ni arranca la unidad."
fi

cat <<EOF

Siguiente:
  - Estado del emparejamiento: sudo -u dyndnsarr $venv/bin/dyndnsarr-agent pair-status
  - Firewall (no se toca): permite el puerto $port solo desde el Hub, p. ej.
      sudo ufw allow from <IP-del-Hub> to any port $port proto tcp
  - Si dejaste agent.env en el clon, bórralo: puede llevar credenciales.
EOF
