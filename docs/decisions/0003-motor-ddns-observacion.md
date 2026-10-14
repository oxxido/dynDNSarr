# ADR-0003 — Configuración, scheduler y consenso de IP del motor DDNS

**Fecha:** 2026-10-14

## Contexto

El slice 2a ([slices/02a-observacion-ip-dns.md](../slices/02a-observacion-ip-dns.md)) añade al Agent la observación de IP pública y DNS. Resuelve A3 y parte de A4 de [architecture.md](../architecture.md) §14 y fija reglas que 2b (escritura en DonDominio) hereda.

## Decisión

1. **Configuración (D1).** Un archivo TOML (`tomllib`, stdlib 3.11) apuntado por `DYNDNSARR_AGENT_CONFIG_FILE`, sin secretos. Sin la variable el motor queda desactivado (comportamiento del slice1); con ella, un archivo ausente o inválido aborta el arranque. Las variables de entorno `DYNDNSARR_AGENT_*` siguen siendo la configuración del proceso (puerto, datos, logs).
2. **Scheduler y SQLite (D2, A3).** Bucle `asyncio` en una tarea del `lifespan`, sin APScheduler ni Celery. `sqlite3` síncrono en el hilo del bucle (sentencias cortas, un solo worker). `httpx` para HTTP y `dnspython` para preguntar a resolvers concretos; ambas son Python puro.
3. **Consenso de IP (D3).** La IP pública está «confirmada» solo si responden ≥2 fuentes, todas las que responden coinciden y es IPv4 global. Cualquier otro caso es `uncertain`: no cambia el estado de IP y se reintenta en el siguiente ciclo. Una IP equivocada, cuando 2b la escriba en DNS, dejaría el dominio apuntando a la nada: se prefiere no concluir antes que concluir mal.

## Consecuencias

- La lista de registros vive en un archivo, no en variables planas; en slice3 el Hub la sustituirá por configuración versionada.
- Un fallo permanente de una fuente de IP deja la IP en `uncertain` indefinidamente; el historial lo muestra y las fuentes son configurables.
- Dos dependencias nuevas en el Agent (`httpx`, `dnspython`), con cota de versión mayor y sin lockfile, como en slice1.
