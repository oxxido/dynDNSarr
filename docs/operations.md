# Operación

Guía de arranque y despliegue (slices 1 a 5). Variables de configuración: [.env.example](../.env.example).

Ambos componentes escuchan por defecto en `127.0.0.1`. Cámbialo solo con un firewall que limite el origen: el Agent no debe ser accesible desde Internet ni por equipos distintos al Hub.

## 1. Desarrollo local

Ver [README.md](../README.md). Cada componente crea su SQLite (`agent.db` / `hub.db`, permisos `0600`) en `DYNDNSARR_<COMPONENTE>_DATA_DIR` y aplica las migraciones al arrancar. Comprobación:

```bash
curl -s http://127.0.0.1:8001/healthz   # {"status":"ok","component":"agent","version":"0.1.0"}
```

`/healthz` responde 503 con `"status":"error"` si la base de datos no responde. Si la base tiene una versión de esquema posterior a la del código, el componente no arranca.

Wheels (lo que se instala en la Raspberry):

```bash
python3 -m pip wheel --no-deps -w dist packages/shared apps/agent apps/hub
scripts/verify-wheels.sh   # construye, instala en un venv limpio y arranca Agent y Hub
```

## 2. Agent en la Raspberry (systemd)

Requiere Python 3.11+ del sistema (ADR-0002), `python3-venv` y `sudo`. Los comandos con `sudo` los ejecuta la persona que administra la Raspberry.

1. Copiar los wheels de `shared` y `agent` (sección 1) a la Raspberry.
2. Usuario y entorno:
   ```bash
   sudo useradd --system --home-dir /var/lib/dyndnsarr --shell /usr/sbin/nologin dyndnsarr
   sudo python3 -m venv /opt/dyndnsarr/venv
   sudo /opt/dyndnsarr/venv/bin/pip install dyndnsarr_shared-*.whl dyndnsarr_agent-*.whl
   ```
   (`pip` descarga FastAPI, Uvicorn y Pydantic desde PyPI.)
3. Configuración (opcional, hay valores por defecto): copiar la sección Agent de [.env.example](../.env.example) a `/etc/dyndnsarr/agent.env` y protegerla:
   ```bash
   sudo install -d -m 0750 -o root -g dyndnsarr /etc/dyndnsarr
   sudo install -m 0640 -o root -g dyndnsarr agent.env /etc/dyndnsarr/agent.env
   ```
   El archivo debe existir aunque esté vacío: la unidad lo exige. Para activar el motor DDNS añade además la configuración de la sección 5.
4. Instalar y arrancar la unidad [apps/agent/systemd/dyndnsarr-agent.service](../apps/agent/systemd/dyndnsarr-agent.service):
   ```bash
   sudo cp dyndnsarr-agent.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now dyndnsarr-agent
   curl -s http://127.0.0.1:8001/healthz
   journalctl -u dyndnsarr-agent -o cat      # logs JSON
   ```
5. Persistencia y recuperación: `sudo systemctl restart dyndnsarr-agent` conserva los datos (`instance_id` igual, `boot_count` +1); `Restart=always` lo relanza tras `sudo kill -9 $(systemctl show -p MainPID --value dyndnsarr-agent)`.

Los datos viven en `/var/lib/dyndnsarr` (creado por systemd, modo `0700`).

## 3. Hub con Docker Compose / Dockge

El Hub se construye desde el repositorio (aún no hay imagen publicada).

```bash
cd apps/hub
docker compose up -d --build        # http://127.0.0.1:8000/healthz
docker compose ps                   # el estado pasa a "healthy"
```

- Por defecto el puerto se publica solo en `127.0.0.1` del servidor. Para la LAN, crear `apps/hub/.env` con `DYNDNSARR_HUB_BIND=<IP LAN del servidor>` (puerto con `DYNDNSARR_HUB_PORT`).
- Los datos están en el volumen `hub-data` (`/data` en el contenedor). El contenedor corre como usuario no root (uid 10001).
- **Dockge:** crear un stack con el contenido de `apps/hub/compose.yaml` y cambiar `build.context` por la ruta absoluta del clon del repositorio en el servidor (el `../..` relativo solo vale dentro del repositorio). Este camino no está verificado en este slice.
- Verificación automática (build, healthcheck, restart y persistencia, con proyecto y puerto propios que se eliminan al terminar): `scripts/verify-compose.sh`.
- El Hub habla **HTTP** y recibe el token de cada Agent: no publiques su puerto a la LAN. Déjalo en `127.0.0.1` (o en la red de Docker del proxy) y pon delante un proxy TLS (§6).
- Consola del Hub (registro de agentes y eventos recibidos), dentro del contenedor: `docker compose exec hub dyndnsarr-hub agent-list` (§6).

## 4. Backup y restauración

Los dos componentes son una SQLite en modo WAL. Las copias contienen datos de configuración: protégelas (`0600`) y no las subas al repositorio.

- **Agent:** con el servicio parado (`sudo systemctl stop dyndnsarr-agent`), copiar `/var/lib/dyndnsarr/agent.db*` (incluye `-wal` y `-shm` si existen) y `/etc/dyndnsarr/agent.toml`. **Antes de actualizar al slice 3a**: la migración 0004 añade columnas a `events` (los eventos ya guardados quedan pendientes de entrega) y no hay vuelta atrás automática. **Antes de actualizar al slice 3b**: la migración 0005 solo crea tablas nuevas (`configuration_versions`, `commands`), también sin vuelta atrás automática. Restaurar: servicio parado, copiar los archivos de vuelta con dueño `dyndnsarr` y modo `0600`, arrancar.
- **Hub (`/data`)**, con el servicio parado:
  ```bash
  docker compose stop hub
  # backup
  docker compose run --rm --no-deps --user root -v "$PWD":/backup hub \
      tar czf /backup/hub-data.tgz -C /data .
  # restauración
  docker compose run --rm --no-deps --user root -v "$PWD":/backup hub \
      sh -c 'tar xzf /backup/hub-data.tgz -C /data && chown -R dyndnsarr:dyndnsarr /data'
  docker compose start hub
  ```
  Estos comandos de backup/restauración siguen el procedimiento estándar de volúmenes, pero no están automatizados ni se probaron en CI.

## 5. Motor DDNS del Agent: observación y actualización (slices 2a y 2b)

El Agent, sin Hub, detecta su IPv4 pública con varias fuentes, pregunta a resolvers externos qué resuelven tus registros y guarda muestras y eventos en su SQLite. Los registros `mode = "monitored"` solo se observan y **nunca se escriben**; los `mode = "managed"` se actualizan en DonDominio cuando la IP pública confirmada difiere de lo que resuelven los resolvers.

### Configuración

1. Copiar [apps/agent/config/agent.example.toml](../apps/agent/config/agent.example.toml) a `/etc/dyndnsarr/agent.toml`, editarlo y protegerlo (no contiene secretos):
   ```bash
   sudo install -m 0640 -o root -g dyndnsarr agent.toml /etc/dyndnsarr/agent.toml
   ```
2. Añadir a `/etc/dyndnsarr/agent.env`: `DYNDNSARR_AGENT_CONFIG_FILE=/etc/dyndnsarr/agent.toml` y `sudo systemctl restart dyndnsarr-agent`.

Sin la variable el Agent arranca como en el slice 1 (motor desactivado). Con ella, un archivo ausente o inválido impide arrancar y el mensaje (en `journalctl -u dyndnsarr-agent` o en consola) indica la clave y el problema.

| Clave | Por defecto | Notas |
|---|---|---|
| `check_interval_seconds` | 300 | Mínimo 60. Se comprueba al arrancar y después cada intervalo. |
| `ip_sources.urls` | — | Mínimo 2, distintas, `https://` (`http://` solo para `localhost`/`127.0.0.1`, pruebas). Deben devolver la IPv4 como texto (≤ 64 bytes). |
| `dns.resolvers` | — | Mínimo 1: `"ip"` o `"ip:puerto"`. Deben ser externos: no uses solo el Pi-hole. |
| `retention.samples_days` | 30 | Muestras de IP y comprobaciones DNS. |
| `retention.events_days` | 365 | Eventos. La purga se hace al final de cada ciclo. |
| `[[records]]` `name`, `mode`, `provider`, `enabled` | `enabled = true` | `mode`: `"monitored"` o `"managed"`. `provider = "dondominio"` es obligatorio en `managed` y no se admite en `monitored`. |
| `update.verify_retry_seconds` | 60 | Mínimo 1. Cada cuánto se comprueba un registro `pending_verification`. |
| `update.verify_timeout_seconds` | 900 | No menor que el anterior. Plazo de propagación antes de `dns_verification_failed`. No está medido: ajústalo tras la prueba manual. |
| `providers.dondominio.url` | `https://dondns.dondominio.com/json/` | Solo para pruebas (`http://` únicamente a `localhost`/`127.0.0.1`). |

### Qué significan los resultados

- **IP pública `confirmed`**: responden al menos 2 fuentes y todas coinciden en una IPv4 pública. Cualquier otra cosa (una fuente caída, discrepancia, IP privada o basura) es **`uncertain`**: no se registra cambio de IP y se reintenta en el siguiente ciclo. Si una fuente está caída para siempre, la IP queda incierta: cámbiala en el TOML.
- **Registro `verified`**: todos los resolvers que responden devuelven exactamente la IP pública. **`mismatch`**: alguno devuelve otra cosa (puede ser transitorio por la caché del resolver hasta el TTL). **`unknown`**: ningún resolver respondió (NXDOMAIN, timeout…). Con la IP incierta no se reclasifica.
- **Eventos** (una fila por *cambio de estado*, no por comprobación): `agent_started`, `ip_changed` (la primera IP confirmada también lo emite, con `previous` nulo), `ip_uncertain` (solo al entrar en incertidumbre), `dns_verified`, `dns_mismatch`, y para `managed`: `dns_update_succeeded` (`ip`, `changed`), `dns_update_failed` (`code`, `retryable`, `attempt`; solo al empezar una racha de fallos o al cambiar el código) y `dns_verification_failed` (`ip`, `waited_seconds`). Llevan `event_id`, `sequence` creciente y `occurred_at` en UTC. Ninguno lleva credenciales ni URL.

### Registros `managed` (DonDominio)

1. Crea antes el registro **A** en la zona, a mano: el Agent no crea registros y un CNAME no sirve (DonDominio responde `The DNS Zone doesn't exist`, error definitivo).
2. Añade las credenciales de DonDNS a `/etc/dyndnsarr/agent.env` (debe ser `0600`, dueño `root`; nunca en el TOML):
   ```
   DYNDNSARR_AGENT_DONDOMINIO_USER=tu-usuario
   DYNDNSARR_AGENT_DONDOMINIO_KEY=tu-clave
   ```
   Un registro `managed` habilitado sin ellas impide arrancar (el mensaje nombra las variables que faltan). Sin registros `managed` no hacen falta.
3. En el TOML: `mode = "managed"` y `provider = "dondominio"`. Reinicia el servicio.
4. **Respalda `agent.db` antes de actualizar** (§4): la migración 0003 reconstruye `record_state` y no hay vuelta atrás automática.

Cómo actúa: con la IP pública **confirmada**, si todos los resolvers que responden devuelven otra IP, hace **una** llamada a DonDominio con esa IP. Que el proveedor acepte no es que el DNS ya resuelva bien:

| Estado del registro | Significa |
|---|---|
| `pending_verification` | El proveedor aceptó la IP; se espera a que los resolvers la devuelvan. No se vuelve a llamar mientras tanto. |
| `verified` | Los resolvers ya devuelven la IP. |
| `mismatch` | Los resolvers devuelven otra IP y no hay una actualización aceptada vigente (fallo del proveedor, espera de reintento o error definitivo). |
| `unknown` | Ningún resolver responde. |

- **Plazo vencido** (`verify_timeout_seconds` sin que el DNS converja): evento `dns_verification_failed` y se vuelve a llamar. Con un CNAME o una zona mal delegada esto se repite cada plazo; revisa la zona.
- **Fallo del proveedor** (5xx, timeout, 429, respuesta ilegible): reintento con espera de 60 s, 120 s, 240 s… hasta 1 h; la espera se guarda y sobrevive a un reinicio. Una IP nueva no hereda la espera de la anterior.
- **Error definitivo** (`auth_failed`: usuario o clave incorrectos; `host_not_found`: el registro A no existe o el dominio no es tuyo): no hay reintentos automáticos para esa IP. Corrige la causa y ejecuta `sync-ddns`, o espera a un cambio de IP. DonDominio no documenta sus códigos de error: un mensaje desconocido se trata como `rejected` y se reintenta (como mucho cada hora) en lugar de bloquear.
- **Límite de tasa:** DonDominio responde 429 si se le llama más de unas 5 veces en pocos segundos. El Agent hace como mucho una llamada por registro y ciclo, con 2 s de pausa entre registros.
- **IP incierta:** nunca se llama.

### Consola

El ejecutable sin argumentos sirve (como systemd). Los subcomandos se ejecutan como el usuario del servicio para poder leer la BD:

```bash
sudo -u dyndnsarr env DYNDNSARR_AGENT_CONFIG_FILE=/etc/dyndnsarr/agent.toml \
    /opt/dyndnsarr/venv/bin/dyndnsarr-agent status
```

| Comando | Qué hace | Código de salida |
|---|---|---|
| `status` | IP pública, estado de cada registro y últimos eventos. Solo lee. | 1 si no hay BD o falta el esquema |
| `check-ip` | Consulta las fuentes ahora y guarda las muestras; no cambia estado ni emite eventos. | 1 si la IP es incierta |
| `check-dns` | Pregunta a los resolvers por cada registro y compara con la última IP confirmada; guarda las muestras. | 0 aunque haya `mismatch`; 1 si falla la configuración |
| `sync-ddns` | Un pase manual de los registros `managed`: observa IP y DNS y actualiza donde haga falta, **ignorando la espera de backoff y el bloqueo por error definitivo** (no la IP ya aceptada dentro de plazo). Hace un pase y sale: no espera a la verificación (la completa el daemon o `check-dns`). Funciona con el daemon parado. | 1 si hay un ciclo del daemon en curso (`ddns.lock`), la IP es incierta o el proveedor falla |

`status` no necesita `DYNDNSARR_AGENT_CONFIG_FILE`; `check-ip`, `check-dns` y `sync-ddns` sí, y `sync-ddns` además las credenciales de DonDominio. Todos leen `DYNDNSARR_AGENT_DATA_DIR` (por defecto `/var/lib/dyndnsarr`). `status` muestra, por registro, la IP aceptada por el proveedor, la próxima espera de reintento y el último error.

`sudo -u` no hereda tu entorno y no conviene poner la clave en la línea de comandos (queda en `ps` y en el historial). Carga `agent.env` como root y cambia de usuario con `runuser`, que conserva el entorno:

```bash
sudo bash -c 'set -a; . /etc/dyndnsarr/agent.env; set +a; \
    exec runuser -u dyndnsarr -- /opt/dyndnsarr/venv/bin/dyndnsarr-agent sync-ddns'
```

Esto exige que los valores de `agent.env` sean válidos también como shell (sin espacios ni caracteres especiales). No se ha probado en la Raspberry en este slice.

El Agent y `sync-ddns` se coordinan con un `flock` sobre `<data_dir>/ddns.lock`: el daemon salta el ciclo si `sync-ddns` está en marcha, y `sync-ddns` sale con 1 si el daemon está en pleno ciclo (reintenta en unos segundos).

### Comprobación manual

- **`monitored`:** con `home.example.com` apuntando ya a tu IP: arranca el Agent, espera un ciclo y ejecuta `status` (el registro debe estar `verified`). Reinicia el servicio: `status` conserva el historial y se hace una comprobación inmediata.
- **`managed`** (con un subdominio de prueba, no el de producción): deja el registro A apuntando a otra IP, configúralo como `managed` y ejecuta `sync-ddns`. Debe salir `pending_verification`; deja el daemon corriendo y repite `status` hasta ver `verified`, y confirma desde fuera con `dig +short ddns.example.com @1.1.1.1`. Anota cuánto tardó y ajusta `verify_timeout_seconds`.

## 6. Enlace Agent → Hub: entrega de eventos (slice 3a)

El Agent entrega al Hub los eventos que guarda en su SQLite (`agent_started`, `ip_changed`, `dns_*`…) con entrega **at-least-once**: cada evento se marca como entregado solo tras el ACK del Hub, que deduplica por `(agent_id, event_id)`. Con el Hub parado los eventos se acumulan en el Agent y, al volver, llegan una sola vez y en orden. El DDNS no depende del Hub: sin él (o mal configurado) el Agent actualiza y verifica igual. Todavía no hay UI: se inspecciona por consola.

El Hub no termina TLS: va **detrás de un proxy TLS** y el Agent verifica siempre el certificado (no hay forma de desactivarlo). El token viaja en una cabecera `Authorization`, por eso `http://` solo se admite hacia `localhost`/`127.0.0.1` (pruebas) y el puerto del Hub no debe estar abierto a la LAN.

### 6.1 Proxy TLS con Caddy y CA local

Receta no probada contra tu infraestructura (criterio manual del slice). En el servidor del Hub, con el Hub publicado solo en `127.0.0.1:8000` (§3) y `hub.example.lan` resolviendo a ese servidor en tu LAN:

```
# Caddyfile
hub.example.lan {
    tls internal            # certificado de la CA local de Caddy
    reverse_proxy 127.0.0.1:8000
}
```

El Agent debe confiar en esa CA: copia la raíz de Caddy a la Raspberry y apúntala con `ca_file` (solo esa CA se acepta; sin `ca_file` se usa el almacén del sistema, para certificados públicos):

```bash
# en el servidor de Caddy (ruta por defecto; en Docker está en el volumen de datos de Caddy)
cat ~/.local/share/caddy/pki/authorities/local/root.crt
# en la Raspberry
sudo install -m 0644 -o root -g dyndnsarr root.crt /etc/dyndnsarr/hub-ca.pem
```

La limitación de tasa por IP la aporta el proxy (el Hub no tiene).

### 6.2 Alta de un agente

Desde el servidor del Hub (la consola corre dentro del contenedor; el token se muestra **una sola vez** y el Hub guarda solo su hash):

```bash
docker compose exec hub dyndnsarr-hub agent-add raspberry-home
```

`agent_id` cumple `^[a-z0-9][a-z0-9-]{0,62}$`. En la Raspberry:

1. `/etc/dyndnsarr/agent.toml`: la sección `[hub]` (ver [agent.example.toml](../apps/agent/config/agent.example.toml)): `url`, `agent_id` y, con CA privada, `ca_file`.
2. `/etc/dyndnsarr/agent.env` (0600): `DYNDNSARR_AGENT_HUB_TOKEN=dda_…`. Con `[hub]` y sin token el Agent no arranca; el mensaje nombra la variable.
3. `sudo systemctl restart dyndnsarr-agent`. El arranque hace una pasada inmediata y, mientras haya pendientes, otra cada 30 s. Al activar `[hub]` se envía el histórico retenido (hasta `retention.events_days`), en lotes de 50.

### 6.3 Consola del Hub

| Comando | Qué hace |
|---|---|
| `agent-add <agent_id>` | Registra el agente y emite su primer token. Error si ya existe. |
| `agent-rotate <agent_id>` | Emite un token nuevo **sin** revocar los anteriores. |
| `agent-revoke <agent_id>` | Revoca todos los tokens salvo el último emitido. Con `--all`, revoca todos y deshabilita al agente (un `agent-rotate` posterior lo reactiva). |
| `agent-list` | Agentes, estado, tokens vigentes y última vez visto. Nunca muestra tokens ni hashes. |
| `events [--agent ID] [--limit N]` | Últimos eventos recibidos (con su IP: es la vista privada). |

**Rotación sin perder eventos:** `agent-rotate` → poner el token nuevo en `agent.env` → `sudo systemctl restart dyndnsarr-agent` → `agent-revoke`. Si se revoca un token en uso, el Agent no pierde nada: retiene los eventos (`auth_failed`) y los entrega al corregir el token.

### 6.4 Estado del enlace y fallos

`dyndnsarr-agent status` muestra los eventos pendientes y rechazados, el último envío correcto y el último error (nunca la URL ni el token):

| Código | Causa habitual |
|---|---|
| `connect_error`, `timeout` | Hub parado o inaccesible. Reintenta con espera de 30 s, 60 s, 120 s… hasta 15 min (±20 %), y se queda en 30 s al recuperarse. |
| `tls_error` | El certificado del proxy no lo firma la CA configurada (falta `ca_file`, CA equivocada o caducada). Los eventos siguen pendientes. |
| `auth_failed` | Token inválido, revocado o de otro agente (401/403). Se registra una vez por racha y se sigue reintentando. |
| `rate_limited`, `server_error`, `http_error`, `bad_response` | 429, 5xx, otro estado o respuesta ilegible. |

Un evento que el Hub rechaza por inválido (`invalid_event`, `payload_too_large`) queda marcado como rechazado y no se reenvía ni bloquea a los siguientes. Los eventos pendientes se purgan igual que los demás a los `retention.events_days`.

### 6.5 Comprobación manual

1. `agent-add`, configurar el Agent con `ca_file` y reiniciarlo: `dyndnsarr-hub events` debe mostrar `agent_started`.
2. Parar el Hub, forzar eventos (reiniciar el Agent, o `sync-ddns` con un cambio de IP), comprobar con `status` que quedan pendientes, arrancar el Hub y esperar hasta unos 15 minutos (o reiniciar el Agent): `events` los muestra **una sola vez**.
3. Quitar `ca_file` (o usar otra CA): `status` debe mostrar `tls_error` y los eventos seguir pendientes.

## 7. Control Hub → Agent (slice 3b)

Desde la consola del Hub (todavía no hay UI) se puede consultar al Agent, enviarle una **configuración versionada** y pedirle **comandos** (`check_dns`, `sync_ddns`). El Agent vive en la Raspberry sin proxy delante: sirve **HTTPS él mismo** con un certificado de una CA local, y el Hub verifica siempre que lo firma esa CA (no hay forma de desactivarlo). El Agent sigue funcionando sin el Hub: DDNS, configuración activa, `config-reset` y la API no dependen de él, y los resultados se entregan al volver (§6).

La API (`/internal/v1`) solo existe si el Agent tiene `DYNDNSARR_AGENT_API_TOKEN` (y `DYNDNSARR_AGENT_CONFIG_FILE`: la configuración remota se superpone sobre el TOML). Con token y escucha fuera de loopback, el Agent **no arranca sin TLS**.

### 7.1 CA local y certificado del Agent

En un equipo de confianza (no hace falta que sea la Raspberry), una sola vez. **La clave de la CA (`agent-ca.key`) no sale de ahí**: ni a la Raspberry ni al Hub.

```bash
# CA local (5 años)
openssl genrsa -out agent-ca.key 4096
openssl req -x509 -new -key agent-ca.key -sha256 -days 1825 \
    -subj "/CN=DynDNSarr Agent CA" -out agent-ca.pem

# Certificado del Agent (1 año). La IP o el nombre con los que el Hub llamará al Agent deben
# estar en subjectAltName: si no, el Hub rechaza el certificado.
openssl genrsa -out agent.key 2048
openssl req -new -key agent.key -subj "/CN=raspberry-home" -out agent.csr
cat > agent.ext <<'EOF'
subjectAltName = IP:192.168.1.50, DNS:raspberry.lan
extendedKeyUsage = serverAuth
keyUsage = digitalSignature, keyEncipherment
basicConstraints = CA:FALSE
EOF
openssl x509 -req -in agent.csr -CA agent-ca.pem -CAkey agent-ca.key -CAcreateserial \
    -days 365 -sha256 -extfile agent.ext -out agent.pem
openssl verify -CAfile agent-ca.pem agent.pem      # agent.pem: OK
```

En la Raspberry:

```bash
sudo install -m 0644 -o root -g dyndnsarr agent.pem /etc/dyndnsarr/agent.pem
sudo install -m 0640 -o root -g dyndnsarr agent.key /etc/dyndnsarr/agent.key
```

En el Hub, la CA **pública** (`agent-ca.pem`) va al volumen `/data`, que es donde `compose.yaml` la busca (`DYNDNSARR_HUB_AGENT_CA_FILE=/data/agent-ca.pem`):

```bash
docker compose cp agent-ca.pem hub:/data/agent-ca.pem
```

**Renovación:** el certificado caduca al año y no hay aviso. Un certificado caducado corta solo el enlace (el Hub ve `tls_error`), no el DDNS. Repite desde `openssl genrsa -out agent.key` hasta `openssl x509 -req` con la misma CA, copia `agent.pem` y `agent.key` y reinicia el Agent. Anota la fecha en tu calendario.

### 7.2 Token de la API y alta en el Hub

```bash
docker compose exec hub dyndnsarr-hub agent-set-api raspberry-home https://192.168.1.50:8001
docker compose exec hub dyndnsarr-hub agent-api-token raspberry-home    # se muestra una sola vez
```

La URL debe ser `https://` y coincidir con un nombre del certificado (§7.1). En `/etc/dyndnsarr/agent.env` (0600):

```
DYNDNSARR_AGENT_HOST=192.168.1.50
DYNDNSARR_AGENT_API_TOKEN=ddh_…
DYNDNSARR_AGENT_TLS_CERT_FILE=/etc/dyndnsarr/agent.pem
DYNDNSARR_AGENT_TLS_KEY_FILE=/etc/dyndnsarr/agent.key
```

Reinicia el Agent y comprueba: `docker compose exec hub dyndnsarr-hub agent-status raspberry-home`. Restringe con el firewall el origen del puerto 8001 al servidor del Hub.

- Este token es **distinto** del que usa el Agent ante el Hub (§6). Aquí el Hub debe presentarlo, así que lo guarda **en claro** en `hub.db` (`/data`, 0600) y no en `.env` (un `.env` de Dockge es visible en su UI y en `docker inspect`). Quien lea `hub.db` o su copia de seguridad puede llamar a la API de ese Agent desde la LAN —solo a esa API limitada, nunca a DonDominio—: protege `/data` y sus backups.
- **Rotación:** `agent-api-token` genera y guarda uno nuevo; hasta actualizar `agent.env` y reiniciar el Agent, el Hub recibe 401 (el DDNS y la entrega de eventos no se ven afectados).
- Con TLS activo, `/healthz` del Agent también es HTTPS: `curl --cacert agent-ca.pem https://192.168.1.50:8001/healthz`.
- **Reloj:** los comandos llevan `expires_at` y se compara el reloj del Hub con el del Agent. Sin NTP en alguno de los dos, un comando puede nacer caducado.

### 7.3 Configuración remota

El Hub puede administrar solo estas secciones: `check_interval_seconds`, `ip_sources`, `dns`, `retention`, `update` y `records`. Son **locales** y el Agent las rechaza (`forbidden_section`): `providers` (la clave de DonDominio viaja en la petición, así que el Hub no puede redirigirla), `hub`, y cualquier secreto, TLS o token. Las credenciales siguen en `agent.env`: un registro `managed` nuevo sin ellas se rechaza (`missing_credentials`).

```bash
# cambio.toml con solo las secciones administrables (mismo formato que agent.toml):
docker compose cp cambio.toml hub:/tmp/cambio.toml
docker compose exec hub dyndnsarr-hub config-push raspberry-home /tmp/cambio.toml
```

Una sección enviada **sustituye entera** a la del TOML (envía, p. ej., todos los `[[records]]` que quieras, no solo el nuevo); las secciones que no envías siguen viniendo del TOML. El Hub crea la versión siguiente (1, 2, 3…), calcula su hash y la envía; el Agent la valida entera y, solo si todo vale, la guarda y la aplica **en el siguiente ciclo** (el ciclo en curso no cambia de configuración).

| Resultado | Significa |
|---|---|
| aplicada | Guardada y activa. Visible en `agent-status` y en `dyndnsarr-agent status` (`Configuración: versión N`). |
| `invalid_config` | No valida; el mensaje indica `ruta: motivo`. El Agent conserva la versión y el comportamiento anteriores. |
| `forbidden_section` | Incluye una sección local o desconocida. |
| `missing_credentials` | Un registro `managed` habilitado sin credenciales de DonDominio en `agent.env`. |
| `hash_mismatch` | El hash no corresponde al contenido. |
| `stale_version` / `version_conflict` | Versión menor que la activa / misma versión con otro contenido. |

| Comando del Hub | Qué hace |
|---|---|
| `config-push <agent_id> FILE.toml` | Crea la versión siguiente y la envía. |
| `config-resend <agent_id>` | Reenvía la última versión creada (idempotente: la misma versión y hash responde «aplicada» sin cambios). |
| `config-show <agent_id>` | Versión deseada, aplicada, último intento y contenido. Nunca muestra tokens. |
| `agent-status <agent_id>` | Consulta `health` y `status` al Agent: configuración activa, IP, registros y enlace. Lleva la IP completa: es la vista privada. |
| `agent-set-api <agent_id> <url>` / `agent-api-token <agent_id>` | Dirección y token de la API del Agent (§7.2). |

**Con el Agent caído** al enviar, la versión queda **pendiente** (`config-show` lo indica); `config-resend` la reenvía cuando vuelva. No hay reconciliación automática hasta el slice 5. El Hub se entera del resultado por la respuesta y, además, por los eventos `config_applied`/`config_rejected` (el outbox entrega cada 30 s).

**Qué manda, el TOML o el Hub:** la versión activa sustituye a las secciones del TOML (el Agent registra una advertencia `config_overrides_toml` si difieren). Si la versión guardada deja de validar al arrancar (por ejemplo, cambias el TOML o quitas credenciales), el Agent **no arranca** y el mensaje apunta a `config-reset`.

**Volver al TOML** (también con el Hub caído): `dyndnsarr-agent config-reset` borra las versiones remotas y emite `config_reset`; **reinicia el Agent** para que use el TOML (el proceso en marcha conserva su configuración en memoria hasta entonces). El Hub ve la versión 0 y puede reenviar con `config-resend`.

### 7.4 Comandos

```bash
docker compose exec hub dyndnsarr-hub command raspberry-home check_dns
docker compose exec hub dyndnsarr-hub command raspberry-home sync_ddns --ttl 120
docker compose exec hub dyndnsarr-hub command-show --refresh <command_id>
```

- `check_dns`: como `dyndnsarr-agent check-dns` (guarda muestras; devuelve el veredicto por registro). `sync_ddns`: un pase como `sync-ddns`, con el mismo `ddns.lock`: si un ciclo lo tiene tomado, termina `failed` con `code: busy` y el Hub debe pedir otro comando. Cualquier otro tipo (p. ej. `wake_device`, que llega en el slice 6) se rechaza como `unsupported_command`.
- El comando se persiste en el Agent **antes** de ejecutarse y se ejecuta en serie. Un `command_id` repetido devuelve el mismo estado y resultado sin reejecutar. `--ttl` (300 s por defecto) fija `expires_at`: un comando caducado no se ejecuta.
- Estados en el Hub: `pending` (aún no aceptado por el Agent), `sent`, `succeeded`, `failed`, `expired`. Si el Agent estaba caído, el comando queda `pending` y **no se reenvía solo**: `command-show --refresh` pregunta al Agent; si no lo conoce, pide uno nuevo.
- Si el Agent se reinicia a mitad de un comando, queda `failed` con `code: interrupted` y **nunca se reejecuta solo** (principio de WoL, arquitectura §5).
- El resultado vuelve como evento `command_completed` (sin IP ni credenciales), también con el Hub caído al enviarlo.

### 7.5 Comprobación manual

1. §7.1 y §7.2 en la Raspberry y el servidor del Hub; `agent-status raspberry-home` debe mostrar `estado ok`.
2. `config-push` de un cambio real (por ejemplo `check_interval_seconds = 120`): «aplicada»; `dyndnsarr-agent status` muestra `Configuración: versión 1`. Repite con un valor inválido (`check_interval_seconds = 5`): «rechazada (`invalid_config`)» y el Agent sigue en la versión 1.
3. `command raspberry-home check_dns` y `sync_ddns`: llegan a `succeeded` con `command-show --refresh`.
4. `dyndnsarr-agent config-reset` + reinicio: `Configuración: TOML local`; en el Hub, `config-show` indica que manda el TOML y `config-resend` lo vuelve a aplicar.

## 8. Landing pública (slice 4)

El Hub sirve una **página pública de estado** (`GET /`) y su JSON (`GET /public/v1/status`), sin login y de solo lectura. Muestra el estado general con la mascota, la última comprobación satisfactoria, las cinco últimas renovaciones (fecha y hora en **UTC** y los dos últimos dígitos de la IPv4), el estado de conectividad y el historial de 7 días. Nunca muestra la IP completa, MAC, dispositivos, tokens ni configuración: sale de un `PublicStatusDTO` con campos de lista blanca y solo lee las tablas `public_*` de `hub.db`, no `events`. La página no se refresca sola: muestra lo del momento de cargarla.

### 8.1 Variables del Hub

| Variable | Por defecto | Efecto |
|---|---|---|
| `DYNDNSARR_HUB_PUBLIC_ENABLED` | `true` | `false`: `/`, `/public/*` y `/static/*` dan el mismo 404 que una ruta inexistente. |
| `DYNDNSARR_HUB_PUBLIC_SHOW_DOMAINS` | `false` | `true`: cada renovación añade el nombre del registro. **Ese nombre resuelve en el DNS público a tu IP completa**: actívalo solo si aceptas que quede a una consulta. |
| `DYNDNSARR_HUB_STATUS_REFRESH_SECONDS` | `60` (mínimo 15) | Cada cuánto consulta el Hub el `status` de cada Agent. |

Con Docker Compose se leen del `.env` junto a `apps/hub/compose.yaml` (ver `.env.example`).

### 8.2 De dónde sale el estado

Una tarea del Hub consulta cada minuto el `GET /internal/v1/status` de cada Agent con API configurada (**§7.2 es requisito**: `agent-set-api` y `agent-api-token`) y guarda una proyección **sin la IP completa**. La petición web nunca llama al Agent. El estado se deriva al leer, con reglas provisionales ([ADR-0007](decisions/0007-landing-publica.md)):

| Estado | Texto | Cuándo |
|---|---|---|
| `healthy` | Operativo | IP confirmada, todos los registros `verified`, observación de menos de 15 min. |
| `degraded` | Degradado | IP `uncertain`, o algún registro sin verificar (`pending_verification`, `mismatch` de menos de 1 h, fallo con reintento). |
| `critical` | Sin conexión | `mismatch` de más de 1 h, o error definitivo de la actualización. |
| `unknown` | Desconocido | No hay proyección, el refresco tiene más de 3 × `STATUS_REFRESH_SECONDS`, la última comprobación o confirmación de IP tiene más de 15 min, o no hay registros habilitados. |

Con varios Agents manda el peor (`critical > degraded > unknown > healthy`). **`Desconocido` es lo correcto cuando no hay observación**: un Agent inalcanzable no significa que Internet haya caído, y nunca se muestra como «Sin conexión». Por eso un Hub recién instalado, un Agent sin API configurada o un Agent apagado dan `Desconocido`; la «Última comprobación» conserva su hora real. Los umbrales (15 min, 1 h, 3 refrescos) no están verificados con datos reales; si cambias `check_interval_seconds` del Agent a más de 15 min, la página quedará en `Desconocido` hasta el slice 5, que los hará configurables.

El historial usa una muestra por refresco (30 días de retención): 84 barras de 2 h, cada una con el peor estado **conocido**; gris donde no se observó nada (el porcentaje observado se indica en el panel). Una renovación es un `dns_update_succeeded`; aparece «Pendiente de verificación» hasta que llega el `dns_verified` del mismo registro. Las renovaciones anteriores a esta versión no se muestran (la lista arranca vacía).

### 8.3 Publicarla sin exponer el resto del Hub

**No publiques el Hub entero**: `/api/v1` (entrega de eventos), `/docs`, `/openapi.json` y `/healthz` no deben ser alcanzables desde Internet. El proxy debe reenviar solo `/`, `/static/*` y `/public/*`. Receta no probada contra tu infraestructura (criterio manual del slice):

```
# Caddyfile
status.example.com {
    @public path / /static/* /public/*
    handle @public {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        respond 404
    }
}
```

El Agent sigue entregando eventos por la ruta privada de §6 (`hub.example.lan`), no por este sitio. El **límite de tasa** lo pone el proxy: el Hub tiene un solo worker y cada petición pública hace unas lecturas SQLite, pero sin límite alguien podría saturarlo.

### 8.4 Comprobación manual

1. Con el Agent real y §7 configurado: `curl -s http://127.0.0.1:8000/public/v1/status` pasa de `unknown` a `healthy` (o `degraded` si hay un registro por verificar) en un minuto; revisa que no aparece tu IP completa.
2. Abre `/` en escritorio y móvil, en los temas Sistema, Claro y Oscuro; la elección persiste al recargar.
3. Apaga el Agent: tras unos 3 minutos la página pasa a `Desconocido` sin tocarla y conserva la hora de la última comprobación.
4. Publícala tras el proxy de §8.3 y comprueba desde fuera que `/api/v1/...`, `/docs` y `/healthz` dan 404.
5. `DYNDNSARR_HUB_PUBLIC_ENABLED=false` y reinicia: `/` y `/public/v1/status` dan 404.
6. Los cuatro estados se ven con la mascota (verde, naranja, roja y gris) y su cara: sonrisa, boca ondulada, ceño y línea recta. Las pupilas siguen al puntero, la mascota flota sobre su sombra y suben burbujas por detrás. Con «reducir movimiento» en el sistema, nada de eso se mueve.
7. Los PNG pesan ~1,5 MB cada uno: optimizarlos queda pendiente ([ADR-0008](decisions/0008-animacion-mascota.md)).

## 9. Hub privado (slice5)

### 9.1 Actualización y aprovisionamiento

Actualiza **primero shared + Agent**, después shared + Hub. Los nuevos `GET /internal/v1/config` y `/internal/v1/diagnostics` exigen el mismo token y TLS de §7. Un Agent anterior sigue aportando estado, pero el Hub indica que no dispone de configuración/diagnóstico compatibles y bloquea su edición inicial. No necesita migrar la BD del Agent; el Hub aplica 0005 y 0006 automáticamente. Respalda `hub.db` con la API de backup SQLite (§4), incluidos los datos privados, antes de actualizar.

Define el origen HTTPS real visto por el navegador, sin ruta:

```dotenv
DYNDNSARR_HUB_ADMIN_ORIGIN=https://dyndnsarr-admin.example.net
```

En Compose se pasa desde el archivo de entorno del despliegue. Fuera de Docker se define al arrancar el proceso. Reinicia el Hub tras cambiar esta variable. Vacía o ausente, el login responde 503 hasta configurarla; la landing y el Agent continúan funcionando. No se confía en `Host` ni `X-Forwarded-*` para decidir este origen.

Crea el único administrador con una terminal interactiva:

```bash
docker compose -f apps/hub/compose.yaml exec hub dyndnsarr-hub admin-create admin
# Fuera de Docker, con el DATA_DIR del Hub:
# .venv/bin/dyndnsarr-hub admin-create admin
```

La contraseña se introduce dos veces sin mostrarla: 12–128 caracteres. El usuario admite 1–64 caracteres (`a-z`, `A-Z`, números, `_`, `.`, `-`). No hay contraseña de fábrica, alta por navegador ni credenciales administrativas por entorno. Solo se guarda el hash Argon2id (64 MiB, 3 iteraciones, 4 hilos). Medición local de desarrollo: aproximadamente 85 ms para hash y 71 ms para verificación; mide en el servidor de destino antes de dimensionar recursos.

Recuperación de contraseña o bloqueo:

```bash
docker compose -f apps/hub/compose.yaml exec hub dyndnsarr-hub admin-password admin
```

Revoca todas las sesiones, restablece el contador de intentos y audita la recuperación; no cambia el usuario. No pongas contraseñas en argumentos, comandos de shell, archivos del repositorio ni mensajes. La CLI debe apuntar a la misma BD/volumen que el Hub en ejecución.

### 9.2 Publicación y sesiones

La administración empieza en `/admin/login`. Requiere HTTPS también en LAN para que el navegador envíe la cookie `__Host-dyndnsarr_session`: `Secure`, `HttpOnly`, `SameSite=Lax`, ruta `/`, sin `Domain`. Dura como máximo 12 h y caduca tras 30 min sin uso; cerrar sesión o cambiar contraseña la revoca en servidor. Reiniciar el Hub conserva sesiones válidas y sus vencimientos. No se guardan cookies en `localStorage`.

Publica la administración por separado de la landing de §8.3. Ejemplo **para aplicar manualmente** en un proxy Caddy externo ya gestionado:

```caddyfile
dyndnsarr-admin.example.net {
    @admin path /admin /admin/*
    handle @admin {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        respond 404
    }
}
```

Ajusta el upstream si el proxy comparte una red Docker; no expongas el puerto HTTP del Hub a Internet ni `/api/v1/agents/*` en este host. El ejemplo no modifica el proxy automáticamente. El host público mantiene su lista blanca de §8.3. El login y el resto de pantallas privadas envían `no-store`, CSP con nonce y `Referrer-Policy: same-origin` (sin referencias a otros orígenes, compatible con formularios y comprobación estricta de `Origin`); `/docs`, `/redoc` y `/openapi.json` ya no se publican en el Hub. `/healthz` conserva el contrato.

Todos los POST (incluidos login/logout) requieren CSRF de la sesión y un origen que coincida con `ADMIN_ORIGIN`; si `Origin` falta se comprueba `Referer`. Un token de servicio no inicia sesión web y la cookie no autoriza la ingestión de eventos. Cinco errores de contraseña en 15 min bloquean nuevos intentos durante 15 min (`429`, `Retry-After`). El contador persiste tras reiniciar, es global para la instalación y no depende de la IP aparente del cliente. Un tercero puede causar un bloqueo temporal: protege el acceso privado desde el proxy y usa la recuperación CLI si procede.

Con `DYNDNSARR_HUB_PUBLIC_ENABLED=false`, `/`, `/public/*` y `/static/*` siguen apagados. `/admin/*`, incluidos sus assets en `/admin/static/*`, funciona de forma independiente.

### 9.3 Pantallas, configuración y acciones

- **Resumen:** estado agregado y por Agent/registro, IPv4 completa con instante de confirmación, último cambio de IP, proceso y enlace/outbox. Un Agent inalcanzable indica `agent_unreachable`, conserva la edad de sus datos y envejece a `unknown`; no acredita que Internet haya caído.
- **Registros:** modo y proveedor, verificación, estado, error y reintento. `monitored` solo observa; DonDominio sigue siendo el único proveedor implementado.
- **Historial:** filtros UTC por Agent, registro (incluidos los retirados) y tipo de evento, paginación estable, instantes observado/recibido y origen manual solo si existe correlación con un comando. Los payloads libres no se vuelcan al navegador. La auditoría muestra actor, recurso, ID, aceptación/resultado y códigos seguros; nunca contraseñas, cookies, CSRF, tokens o cuerpos de configuración.
- **Diagnóstico:** muestras almacenadas por fuente IPv4 y resolver externo, direcciones/errores, aceptación del proveedor y última verificación DNS. «Actualizar diagnóstico» solo consulta la caché del Agent; «Comprobar DNS» envía `check_dns` y produce observaciones nuevas. El Hub no resuelve DNS ni prueba destinos desde el navegador.
- **Configuración:** parte de la efectiva del Agent, también cuando su versión es 0 (TOML). Edita registros, habilitado/modo/proveedor, frecuencia, fuentes/resolvers, verificación y retención local. Borrar el nombre retira el registro de la propuesta; cambiar explícitamente a `managed` autoriza actualizarlo y requiere credenciales locales. Una configuración inválida conserva la anterior. Las credenciales, `providers`, `hub`, endpoints, tokens y CA siguen en la Raspberry/CLI/entorno y no se editan aquí.

La última configuración **deseada** puede estar pendiente o rechazada; no equivale a la aplicada. Una edición obsoleta da 409 y exige recargar. Un timeout mantiene la versión pendiente; reenviarla conserva versión y hash. Un doble envío del mismo formulario devuelve la operación ya creada; cambiar su contenido reutilizando el ID da 409.

Las acciones tienen alcance de **Agent**, no de un solo registro. `sync_ddns` incluye únicamente `managed` habilitados; no se ofrece sin ellos y el Agent repite la comprobación. Los GET nunca lanzan acciones. El seguimiento del comando diferencia aceptación y ejecución; una respuesta perdida se consulta por el mismo `command_id` o se reconcilia al recibir el evento, sin reejecutar. Los POST web normales redirigen tras éxito; el API de respaldo vive en `/admin/api/v1` y exige la misma sesión y CSRF (no es todavía el API de terceros/MCP).

El refresco periódico de §8 guarda por separado cachés privadas y proyección pública. Las pantallas leen esas cachés y muestran su edad. Se recoge como máximo una página de 100 muestras por Agent y pasada, continuando el cursor en la siguiente; con muchas observaciones la lista/contadores pueden ser parciales. Los errores de las consultas nuevas no impiden proyectar el `status` público disponible. La IP completa nunca se copia a `public_*`.

### 9.4 Estados, cobertura y métricas

La UI permite guardar tres umbrales globales del Hub: antigüedad máxima de observación (900 s por defecto, hasta 86400), mismatch crítico (3600 s, hasta 2592000) y factor de refresco obsoleto (3, rango 1–100). La antigüedad no puede ser menor que el mayor intervalo de comprobación conocido; se avisa si falta la configuración de algún Agent. Los valores sobreviven al reinicio, se auditan y afectan a nuevas evaluaciones públicas y privadas. El historial conserva su política original. Los demás ajustes del proceso/publicación siguen por entorno y requieren reiniciar.

Ventanas: 24 h, 7 d y 30 d. Se muestran duración `healthy/degraded/critical/unknown`, cobertura y porcentaje saludable sobre **tiempo conocido**, con numerador/denominador. Por ejemplo, 1 h saludable en una ventana de 24 h da 100 % sobre 1 h conocida y cobertura 4,17 %; sin tiempo conocido no hay porcentaje.

La evidencia conserva su timestamp original, pero su cobertura empieza al recogerla en el Hub y termina como máximo al vencer el intervalo del Agent (limitado por la antigüedad configurada). No se rellena retrospectivamente una caída del Hub. Una lectura repetida no crea otro check ni prolonga el tiempo disponible. Reinicios, fallos explícitos, registros retirados, datos anteriores a la actualización y huecos quedan desconocidos. Se purgan muestras/observaciones privadas de más de 30 días por lotes acotados; la retención general de eventos/auditoría del Hub corresponde al slice6.

Se distinguen salud del servicio, respuestas de fuentes IPv4 y respuestas de resolvers DNS. Estas últimas métricas miden respuestas observadas, no disponibilidad independiente de Internet ni coincidencia con la IP esperada (que se ve por registro). Los grupos parciales no acreditan éxito. Cada muestra de fuente/resolver cuenta una vez; no se promete un total exhaustivo de comprobaciones del Agent cuando el Hub estuvo apagado o no recogió todas las páginas.

Las incidencias son tramos observados consecutivos degradados/críticos. Solo un estado saludable contiguo confirma recuperación; `unknown` corta la evidencia y deja el final sin recuperación confirmada. Las barras públicas de 7 d conservan su contrato anterior: su cobertura por barras no es el porcentaje privado por duración.

### 9.5 Comprobación manual en la instalación

1. Tras backup y actualización Agent → Hub, configura el origen HTTPS, aprovisiona el administrador y comprueba que una ventana sin sesión no obtiene HTML/JSON privados. La landing pública debe seguir sin IP completa, usuario o tokens.
2. Inicia sesión, revisa cookie, cierre de sesión y rechazo al reutilizarla. Prueba la recuperación CLI y el bloqueo temporal sin publicar credenciales.
3. Revisa resumen, registros, diagnóstico, historial y configuración en escritorio/móvil y en Sistema/Claro/Oscuro. Comprueba teclado/foco, persistencia del tema y fechas UTC.
4. Con un Agent real, espera al refresco o pulsa «Actualizar diagnóstico»; identifica fuentes y resolvers reales, edad de datos, IP completa y distinción aceptación/verificación. Apaga el Agent y comprueba `agent_unreachable` y el paso a desconocido.
5. Envía un cambio inválido de frecuencia y comprueba el rechazo y conservación de la activa. Envía uno válido y verifica versión/hash/ack; repite el envío para comprobar que no duplica. Ejecuta `check_dns` y consulta su resultado.
6. Solo con un registro `managed` real de prueba, comprueba `sync_ddns` y que los `monitored` siguen sin escritura. Esta mutación no se ha ejecutado automáticamente contra DonDominio.
7. Reinicia el Hub: configuración, política y sesiones válidas se conservan; los huecos del uptime no se rellenan. Apaga la landing pública y comprueba que la administración sigue disponible.

## 10. Integraciones y entrega v1.0 (slice6)

### 10.1 Actualización e instalación limpia

Haz un backup consistente (§4), conserva los archivos locales/secretos y actualiza primero el
Agent y después el Hub. Se aplican automáticamente Agent 0006 y Hub 0007, sin borrar tablas.
Para volver a una versión anterior restaura el backup: un binario antiguo rechaza un esquema nuevo.
Los archivos de ejemplo no activan permisos WoL ni cambian tu `.env`.

Construcción verificable desde un clon limpio con Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/pip install -e packages/shared -e apps/agent -e apps/hub -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest
scripts/verify-wheels.sh
.venv/bin/pip wheel --no-deps -w dist ./packages/shared ./apps/agent ./apps/hub
```

Instala los wheels de `shared` y `agent` en el venv dedicado de la Raspberry según §2; copia la
configuración, instala la unidad systemd y aprovisiona TLS/tokens según §6–7. Estos cambios del
sistema los ejecuta su administrador. No hace falta root, raw socket ni capacidades para WoL UDP
o la comprobación TCP. El Hub se construye en Compose/Dockge según §3. Mantén `/data` persistente,
HTTPS externo y la escucha restringida; crea el administrador por CLI (§9). La API `/healthz`
sigue compatible y hay un worker por componente.

El workflow `release.yml` verifica versión, pertenencia del tag a main, suite, wheels y Docker antes
de publicar. Tras merge autorizado, el mantenedor puede crear/push `v1.0.0`; se adjuntan los tres
wheels y `SHA256SUMS`, y se publica `ghcr.io/oxxido/dyndnsarr/hub:1.0.0` (imagen Linux amd64 del
runner). Hasta que ese workflow termine correctamente, construye desde el repositorio; no asumas
que el tag o la imagen existen. En Dockge, después de verificar la publicación, puedes sustituir
`build` por la imagen semántica publicada. No se crea ningún tag/release al abrir el PR del slice.

### 10.2 Pi-hole v6 y políticas WoL en el Agent

Copia [integrations.example.toml](../apps/agent/config/integrations.example.toml) como archivo local
del Agent, con permiso de lectura para su usuario, y declara `DYNDNSARR_AGENT_INTEGRATIONS_FILE`
en su entorno. Es independiente del TOML DDNS y no se puede editar mediante config-push o la UI.
Para desactivar Pi-hole elimina su sección; puedes mantener solo políticas WoL.

- Fija URL de Pi-hole v6 y allowlist mínima de redes. Preferir HTTPS; una CA local puede indicarse
  en `pihole.ca_file`. HTTP solo se permite a una dirección local explícitamente autorizada.
- Configura `DYNDNSARR_AGENT_PIHOLE_PASSWORD` con una contraseña de aplicación Pi-hole v6.
  El SID permanece solo en memoria; el adaptador usa POST de autenticación y GET de inventario y
  concesiones. No modifica DNS/DHCP. Pi-hole v5 no está soportado.
- Las lecturas tienen timeout, respuesta máxima 1 MiB y hasta 500 dispositivos. Un fallo conserva
  el último inventario con estado desconocido, código seguro y fecha del último éxito. Hay backoff
  hasta 900 s. Si el archivo local es inválido, se desactiva la integración y DDNS sigue funcionando.
- En cada dispositivo fija ID único, MAC, IP LAN esperada, broadcast, puerto UDP y puerto TCP de
  comprobación. Los permisos manual/automático son `false` por defecto; cambia cada uno de forma
  explícita. No se aceptan destinos enviados por el navegador.
- `manual=true` habilita el botón privado. La CLI equivalente es
  `dyndnsarr-hub command <agent_id> wake_device --resource-id nas`; el token/CA siguen en la configuración.
- `automatic=true` permite WoL tras arranques posteriores al primero del Agent. Antes comprueba el
  puerto TCP; si responde no envía UDP. Reserva intento en SQLite antes de enviar y aplica cooldown
  persistido de al menos 300 s (default 900), incluso ante reinicio o respuesta perdida.

En el Hub activa `DYNDNSARR_HUB_INTEGRATIONS_ENABLED=true`, reinicia y abre `/admin/integrations`.
El Hub recoge snapshots cada 15 s con TLS/token; el navegador solo lee la caché. Un Agent antiguo
sin `/internal/v1/integrations` muestra inventario no disponible y conserva su DDNS/estado habitual.

Estados de inventario: `known` acredita conocimiento (incluida una concesión DHCP), `likely_active`
una consulta DNS en los últimos 300 s, `stale` actividad anterior y `unknown` falta de evidencia
actual. Solo la conexión TCP positiva de un dispositivo autorizado da `active`. Una concesión no
prueba conexión, ni un TCP fallido demuestra apagado. WoL distingue `sent`, `booting`, `reachable`
y `unconfirmed`; la ventana de arranque por defecto es 180 s. Comprueba BIOS/NIC, VLAN/broadcast y
el puerto real en tu hardware; el software no puede garantizar el encendido.

### 10.3 SMTP, webhooks y Slack

Copia [alerts.example.toml](../apps/hub/config/alerts.example.toml) al volumen protegido del Hub
(por ejemplo con `docker compose cp alerts.toml hub:/data/alerts.toml`, como usuario propietario del
volumen), fija `DYNDNSARR_HUB_ALERTS_FILE=/data/alerts.toml` y reinicia. Para habilitar SMTP descomenta
su sección y añade `smtp` a los canales de las reglas. Sin archivo no hay alertas.

- URL webhook por `DYNDNSARR_HUB_WEBHOOK_URL` (secreto completo); payload `{"text":"…"}` compatible
  con Slack. TLS verificado, sin redirects/proxy automático; para receptores LAN se exige allowlist.
- SMTP implícito TLS por defecto (465), o `starttls=true` con puerto 587 y STARTTLS obligatorio.
  Usuario/contraseña por `DYNDNSARR_HUB_SMTP_USER` y `DYNDNSARR_HUB_SMTP_PASSWORD`; CA local opcional.
- Las reglas seleccionan eventos, canales, severidad, espera mínima y supresión. El umbral comienza
  al recibir el evento en el Hub; no afirma duración durante un hueco sin observaciones. Una
  recuperación explícita cancela avisos pendientes de esa incidencia y puede emitir el aviso de
  recuperación. Sin `recovery_events`, un evento es puntual y no se repite indefinidamente.
- `ip_uncertain` significa fuentes sin consenso, no caída de Internet demostrada; `ip_recovered`
  registra que vuelven a confirmar IPv4, incluso si no cambió. Pi-hole emite salud al cambiar y WoL
  registra su resultado. El aviso indica regla/severidad/tipo/fecha/ID, sin volcar el payload.
- Hasta 10 000 entregas pendientes y diez por pasada, ocho intentos con backoff 30–3600 s;
  tras agotarlos el resultado es `failed`. La cola/auditoría sobreviven a reinicios. Una respuesta
  perdida puede duplicar un aviso con el mismo Idempotency-Key/Message-ID; el receptor decide cómo
  deduplicarlo. No se garantiza notificar si Hub/Agent no pueden ejecutarse o no tienen conectividad.

`/admin/alerts` muestra reglas/entregas y ofrece prueba con sesión, CSRF, origen e ID de operación;
un doble envío del mismo formulario no crea dos pruebas. No muestra destinos secretos ni errores
remotos libres. Si el archivo es inválido, la página lo indica y el resto del Hub continúa.

### 10.4 Homarr, exportación y retención

Homarr puede usar su [widget iframe](https://homarr.dev/docs/widgets/iframe/) con
`https://<hub-publico>/public/homarr`. Configura `DYNDNSARR_HUB_HOMARR_ORIGIN` con el origen HTTPS
exacto de Homarr y `DYNDNSARR_HUB_APPLICATION_URL` con la URL pública de la aplicación. Amplía el
proxy de §8 para permitir únicamente los GET de `/public/homarr` y `/public/v1/homarr`.
El JSON tiene estado, última comprobación, último cambio IP observado y enlace; no lleva IP
completa, MAC, dispositivos o datos administrativos. No usa cookie/token ni llama a Homarr. Con
`PUBLIC_ENABLED=false` ambos endpoints dan 404. El admin nunca permite iframe.

Exporta metadatos de diagnóstico mediante `GET /admin/api/v1/export` autenticado: JSON de hasta
100 eventos por página, filtros `agent`, `record`, `type`, `since`, `until`, `cursor`, `limit`.
Conserva timestamps de observación/recepción, IDs/tipo/registro, sin payloads/configuración/secretos.
`next_cursor` permite continuar; conserva los filtros al pasarlo. Usa la sesión del navegador,
no tokens de servicio ni credenciales en la URL. El JSON se descarga con `no-store`.

`DYNDNSARR_HUB_RETENTION_DAYS` (365 por defecto) controla eventos recibidos, auditoría, entregas
finales, señales cerradas y comandos finales, con purga de hasta 500 filas por tabla cada pasada.
Conserva entregas pendientes y comandos activos. Configuraciones activas/deseadas no se purgan.
Los snapshots son uno por Agent y los límites previos de observaciones/métricas siguen vigentes.
`DYNDNSARR_HUB_RECEIPTS_DAYS` (400) conserva tombstones de deduplicación tras borrar el historial;
debe ser al menos la retención del Hub **y cubrir la mayor retención de eventos de sus Agents**.
Fuera de esa ventana, un evento muy antiguo puede volver a considerarse nuevo. El Agent mantiene
su retención existente: los eventos, incluidos pendientes del outbox, expiran según `events_days`.

### 10.5 Comprobación en la instalación real

1. Tras backup y upgrade Agent → Hub, verifica healthchecks, reinicio, login y persistencia.
2. Con Pi-hole v6 propio confirma nombres/IP/MAC/concesiones solo en privado y consulta satisfecha;
   interrumpe la integración y comprueba que DDNS sigue y el inventario envejece a desconocido.
3. En un equipo de prueba con permisos manuales, envía WoL, repite el formulario y reinicia el Agent:
   comprueba cooldown, resultado incierto y accesibilidad por el puerto configurado. Habilita auto
   solo cuando conozcas su comportamiento tras reinicio. No apaga ni reinicia dispositivos.
4. Prueba SMTP/webhook propios, interrumpe el receptor y verifica reintento/recuperación y auditoría.
5. Revisa dispositivos/alertas en móvil/escritorio y temas; exporta una página y verifica ausencia de
   secretos. Incrusta el widget en Homarr y comprueba que desactivar lo público lo apaga.

Estas pruebas con hardware, credenciales y configuración del servidor las realiza su administrador.
La validación automática usa servicios falsos/TLS local y no envía WoL o avisos a destinos reales.

## 11. API de servicios, MCP y Caddy (slice7)

### 11.1 Actualizar y habilitar la API

Actualizar Agent, shared y Hub a 1.1.0. Hacer primero backup coherente de SQLite (§4):
la migración Hub 0008 añade identidades, intenciones, publicaciones, revisiones y sondeos.
Un Agent anterior rechaza `resource_id` DNS; nunca se degrada la petición a un comando global.
La consola/UI v1.0 mantiene comandos de Agent completo si no especifica un registro.

`DYNDNSARR_HUB_SERVICES_ENABLED=true` habilita `/service/v1`; false (default) responde 404.
Publicar esta ruta solo por HTTPS en el proxy privado ya gestionado. No usar la ruta pública
restringida de §8.3 para ello, ni exponer el Agent. El Hub sigue detrás del proxy, sin TLS propio.
La API usa Bearer de cliente, no cookie web ni token `dda_`/`ddh_`; rechaza peticiones con Origin.
Esto permite integraciones de servicio, no llamadas desde JavaScript de un sitio web.

Copiar y adaptar `apps/hub/config/service-client.example.toml`. Acciones, Agents, dominios,
dispositivos, destinos y puertos son listas exactas; las listas vacías no conceden acceso.
Cada acción necesita estar enumerada: `read`/`operate`/`publish` son categorías del catálogo,
no scopes que puedan expandirse. Los permisos no se pueden cambiar mediante la API.

```sh
# En Docker, copiar primero el archivo de permisos al volumen /data.
docker compose exec hub dyndnsarr-hub client-add assistant /data/service-client.toml
docker compose exec hub dyndnsarr-hub client-list
# Revocar inmediatamente todas las solicitudes posteriores de esta identidad:
docker compose exec hub dyndnsarr-hub client-revoke assistant
```

Guardar el token `dds_` mostrado una sola vez en el entorno secreto del cliente. SQLite
solo conserva su hash. Para rotar o cambiar permisos, crear otra identidad, actualizar el
cliente y revocar la anterior. No se reutiliza un ID revocado. La revocación no elimina
publicaciones ni cancela intenciones ya aceptadas; retirarlas explícitamente si corresponde.
Revisiones/intenciones se conservan para rollback/deduplicación; incluirlas en el backup y
vigilar el tamaño. La auditoría sigue la retención general del Hub.

### 11.2 Consumir por API o MCP

`GET /service/v1/tools` devuelve solo herramientas concedidas, con su JSON Schema.
`POST /service/v1/tools/{nombre}` recibe JSON estricto, máximo 16 KiB. `401` significa
credencial inválida/revocada, `403` permiso insuficiente, `409` conflicto/política y `400`
entrada inválida. Los errores contienen solo `code`, sin respuestas de proveedores.
Las consultas también usan POST para mantener un catálogo idéntico en MCP.

Consultas: `get_status`, `list_domains` (`agent_id`), `dns_history` (`agent_id`, `domain`,
`limit` 1–100 y `cursor` opcional), `list_devices` (`agent_id`), `list_publications` (`{}`),
`publication_history` (`publication_id`, últimas 100 revisiones) y `get_operation` (`operation_id`).
El historial se filtra por Agent/dominio antes de paginar. Estado e inventario provienen de
caché con fecha y `stale`; no disparan red. Las publicaciones muestran configuración y
sondeo del upstream por separado. La accesibilidad comienza como `unknown`.

Operaciones DNS: `check_dns`/`sync_ddns` con `agent_id`, `domain` exacto y `operation_id` UUID.
El Agent valida de nuevo al recibir/ejecutar; `sync_ddns` rechaza `monitored`. `wake_device`
usa `agent_id`, `device_id`, UUID y política WoL local, inventario reciente y cooldown.
Un comando aceptado no significa terminado: consultar `get_operation`; el outbox actualiza
el resultado. Si el envío queda ambiguo, repetir la misma petición/UUID conserva command_id.
No crear otro UUID para resolver un timeout. Un UUID con otro cliente/acción/contenido da 409.

MCP: instalar el wheel Hub (incluye SDK oficial `mcp>=1.28,<2`) en el entorno del cliente.
Configurar un servidor **stdio** cuyo comando sea la ruta absoluta a `dyndnsarr-mcp`, sin
argumentos. Pasar por el mecanismo de secretos del cliente:

```text
DYNDNSARR_HUB_MCP_HUB_URL=https://dyndnsarr-api.example.net
DYNDNSARR_HUB_MCP_TOKEN=<token emitido por client-add>
DYNDNSARR_HUB_MCP_CA_FILE=<CA local si no se usa un certificado público>
```

`MCP_CA_FILE` es opcional; sin ella se usa el trust store del sistema. HTTP solo se acepta
en loopback para pruebas locales. El proceso MCP no necesita directorio de datos, acceso a
SQLite ni credenciales del Agent. stdout está reservado al protocolo; errores seguros a stderr.
No se publica un endpoint `/mcp` HTTP, no hay flujo OAuth ni SSE propio. Los clientes que
aceptan servidores stdio usan inicialización/listado/llamada estándar del SDK MCP.

### 11.3 Aprovisionar Caddy manualmente

Esto es una guía para el operador: la aplicación no instala Caddy, abre puertos ni cambia
su configuración global. No aplicar el ejemplo completo encima de una instalación existente.
Integrar el bloque reservado de `apps/hub/config/caddy.example.json` en un servidor Caddy
existente llamado `gateway`, con `listen=[":443"]` y HTTPS automático habilitado:

```json
{"@id":"dyndnsarr","handle":[{"handler":"subroute","routes":[]}]}
```

El bloque no puede tener matchers, `terminal` ni manejadores adicionales. Debe ser alcanzable
antes de rutas catch-all; las rutas avanzadas/regex no se gestionan desde DynDNSarr.
No editar manualmente su contenido: el reconciliador lo repone según SQLite. Las demás rutas
se conservan. No compartir un dominio/wildcard en otros bloques/servidores. Se rechazan esos
conflictos; un ETag cambiado devuelve una intención pendiente y se vuelve a leer.

Mantener el admin API de Caddy aislado. Para acceder desde el contenedor Hub, usar un proxy
**HTTPS privado autenticado** delante de ese admin API, con controles de red. El Bearer
`DYNDNSARR_HUB_GATEWAY_TOKEN` lo debe verificar ese proxy: Caddy admin nativo no lo verifica.
Copiar su CA al volumen del Hub y fijar `DYNDNSARR_HUB_GATEWAY_CA_FILE`; sin ella, trust store
del sistema. HTTP solo en loopback (que en Docker es el propio contenedor). No exponer 2019
a Internet ni publicar un proxy admin sin autenticación. Limitar las credenciales a las rutas
necesarias: lectura `/config/apps/http/servers`, PATCH de las rutas del servidor autorizado.

Adaptar `apps/hub/config/gateway.example.toml`, copiarlo al volumen y fijar
`DYNDNSARR_HUB_GATEWAY_FILE=/data/gateway.toml`. Sin archivo, gateway desactivado. Archivo
inválido impide arrancar, para no operar con una política supuesta. Reiniciar el Hub tras
cambiar política. `admin_networks` autoriza las IP del admin API; se resuelven y fijan antes
de conectar conservando Host/SNI. Los destinos publicados son IPv4 literales por ID con
puertos enumerados. Siempre se bloquean metadatos/link-local/multicast/unspecified; no se
aceptan URLs, DNS, rutas, credenciales o dial arbitrario desde una herramienta.

Para upstream HTTPS, el certificado debe ser verificable por Caddy y tener SAN de la IP;
no hay opción insecure. Caddy gestiona certificados públicos y redirección HTTP→HTTPS:
el DNS y los puertos necesarios deben estar preparados por el operador. DynDNSarr no crea
ni modifica registros DNS al publicar. Usar `caddy run --resume` con almacenamiento persistente
según el despliegue existente; recargar un Caddyfile antiguo puede reemplazar cambios API.
El reconciliador repone su bloque, pero no restaura el resto de una configuración perdida.

### 11.4 Publicaciones, caducidad y recuperación

Conceder `publish` y los dominios/destinos/puertos concretos en el cliente. Ejemplo del cuerpo
para `publish` (UUID nuevo únicamente para una intención nueva):

```json
{
  "operation_id": "3a0d470c-c920-4721-bd16-8072072c7f2f",
  "publication_id": "app",
  "expected_revision": 0,
  "name": "Aplicación de desarrollo",
  "domain": "app.example.net",
  "destination": "app",
  "port": 8080,
  "access": "public",
  "enabled": true,
  "expires_at": "2027-01-01T18:00:00Z"
}
```

Crear usa revisión 0; para modificar, usar la revisión actual. La revisión aumenta al aplicar,
deshabilitar, retirar, expirar o hacer rollback. `expires_at` es opcional, UTC canónica futura;
null permite duración indefinida. `access=private` exige `private_networks`: Caddy permite
solo IPs de conexión dentro de esos CIDR y responde 403 al resto. No es login de usuario.
Si hay otro reverse proxy delante, Caddy verá la IP de ese proxy: no usar esta modalidad
como restricción por visitante sin conexión directa. No se confía en X-Forwarded-For.

`disable_publication`/`delete_publication` reciben publication_id, expected_revision y UUID.
`rollback_publication` añade `revision`: recupera esa revisión solo si sigue permitida y no
ha caducado. El historial conserva configuraciones propias, no secretos ni snapshots completos
de Caddy. La API del proxy valida/aplica cada cambio atómicamente; un rechazo definitivo
conserva la versión previa. Las intenciones ambiguas quedan `pending` hasta comprobar Caddy.
Consultar/repetir el mismo UUID; otro contenido con él no se acepta. No confundir `configured`
con accesibilidad: `verify_publication` sondea únicamente el upstream, sin redirects, y guarda
la fecha/resultado; el HTTPS público queda explícitamente `unknown` hasta la prueba manual.
Cambiar una publicación invalida su sondeo anterior.

Cada 15 s y al arrancar, el Hub reconcilia su bloque: no reactiva publicaciones caducadas,
deshabilita destinos retirados de la política y repara deriva. Mantiene conexión/tarea propias,
sin bloquear DDNS. **La retirada puede retrasarse mientras Hub o Caddy estén apagados**;
una caducidad de aplicación no sustituye una barrera de seguridad temporal externa.

### 11.5 Comprobación manual en la instalación

Tras backup y actualización, con recursos de prueba autorizados:

1. Conectar un cliente MCP de lectura: listar solo sus herramientas/recursos; intentar una
   operación y un Agent ajeno debe dar rechazo. Revocar token y comprobar rechazo en la sesión abierta.
2. Delegar un registro managed de prueba y comprobar que `sync_ddns` solo lo toca a él;
   repetir UUID y comprobar un solo comando. Un monitored no admite escritura.
3. Aprovisionar un bloque Caddy vacío sin modificar rutas existentes. Publicar un dominio/destino
   permitidos; comprobar HTTPS/certificado desde fuera y conservación de los otros sitios.
4. Probar dominio/destino/puerto no permitidos, conflicto y edición obsoleta: no deben reemplazar
   publicaciones. Cambiar destino válido y hacer rollback; verificar el destino anterior.
5. Publicar con caducidad cercana, detener/rearrancar Hub y comprobar retirada. Simular Caddy
   inaccesible, volverlo a habilitar y comprobar reconciliación sin publicaciones duplicadas.
6. Probar `access=private` desde un CIDR permitido y desde fuera; revisar auditoría privada
   y confirmar que landing/Homarr no muestran servicios, destinos, credenciales ni dispositivos.

Las pruebas automatizadas usan Caddy simulado y MCP/Hub TLS reales; las comprobaciones de
Caddy/ACME y destinos de producción requieren esta validación manual del operador.
