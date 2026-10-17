# Operación

Guía de arranque y despliegue (slices 1 y 2a). Variables de configuración: [.env.example](../.env.example).

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

## 4. Backup y restauración

Los dos componentes son una SQLite en modo WAL. Las copias contienen datos de configuración: protégelas (`0600`) y no las subas al repositorio.

- **Agent:** con el servicio parado (`sudo systemctl stop dyndnsarr-agent`), copiar `/var/lib/dyndnsarr/agent.db*` (incluye `-wal` y `-shm` si existen) y `/etc/dyndnsarr/agent.toml`. Restaurar: servicio parado, copiar los archivos de vuelta con dueño `dyndnsarr` y modo `0600`, arrancar.
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
