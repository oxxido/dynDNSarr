# Operación

Guía de arranque y despliegue (slice 1). Variables de configuración: [.env.example](../.env.example).

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
   El archivo debe existir aunque esté vacío: la unidad lo exige.
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

- **Agent:** con el servicio parado (`sudo systemctl stop dyndnsarr-agent`), copiar `/var/lib/dyndnsarr/agent.db*` (incluye `-wal` y `-shm` si existen). Restaurar: servicio parado, copiar los archivos de vuelta con dueño `dyndnsarr` y modo `0600`, arrancar.
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
