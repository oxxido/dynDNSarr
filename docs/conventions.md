# Convenciones de código

Derivadas de [architecture.md](architecture.md) §0, §2 y §4. Si algo no está aquí ni allí, se decide en el slice y se anota.

## Stack

- Python 3.12+ (verificar en la Raspberry destino). FastAPI/Uvicorn, Jinja2, HTMX solo donde aporte, SQLite por componente.
- No añadir Redis, PostgreSQL, MQTT, Celery, React, Node.js ni microservicios sin una necesidad real documentada.
- Evitar ORM pesado: `sqlite3` con repositorios y migraciones explícitas, si basta.
- Un solo worker por proceso (evita schedulers duplicados).

## Estructura

- Monorepo: `apps/agent`, `apps/hub`, `packages/shared` (contratos Pydantic versionados, sin secretos).
- No mezclar infraestructura HTTP, reglas DDNS y acceso SQLite en un mismo archivo.

## Reglas de dominio

- El Agent nunca depende del Hub para el DDNS. Pi-hole, Homarr, correo y web son opcionales.
- El Hub no modifica DNS; pide operaciones al Agent.
- La vista pública usa un DTO propio. Nada de IP completa, MAC, nombres de dispositivo ni secretos.
- Ningún indicador operativo sale de datos inventados: viene de una observación con marca temporal o es `unknown`.
- Registros `monitored`: nunca se escribe.

## Seguridad

- Sin secretos en repositorio, logs, eventos ni exportaciones.
- TLS siempre verificado: prohibido `verify=False`.
- Sin comandos arbitrarios del sistema ni destinos no autorizados.
- Operaciones de modificación nunca por GET.

## Pruebas

- Pruebas automatizadas antes de integrar con DonDominio real o exponer endpoints administrativos.
- Cada slice entrega código, tests, configuración de ejemplo, instrucciones de arranque, migraciones y criterios de aceptación demostrables.
- No inventar el API de DonDominio: verificar la documentación oficial antes de codificar el adaptador.

## Pendiente de fijar en slice1

Linter/formatter, herramienta de migraciones, idioma de identificadores y mensajes (la arquitectura ya usa nombres en inglés para código y paquetes), gestor de dependencias.
