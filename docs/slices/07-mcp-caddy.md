# Slice 7 — API de servicios, MCP y Caddy (v1.1)

- **Estado:** en implementación
- **Fase (arquitectura):** 6
- **Rama:** `slice/07-mcp-caddy`
- **PR:** —

## Objetivo

Permitir consultar y operar DynDNSarr mediante MCP con identidad revocable y permisos
explícitos, y publicar servicios HTTP/HTTPS mediante Caddy sin ampliar esos permisos ni
comprometer las rutas existentes. El Agent conserva su autonomía DDNS.

## Alcance

- Incluye API versionada de servicios, MCP stdio con SDK oficial, aprovisionamiento CLI,
  consultas de estado/dominios/historial/dispositivos/publicaciones y operaciones DNS/WoL;
  comandos DNS dirigidos a un registro; publicaciones con revisión, idempotencia, creación,
  modificación, desactivación, eliminación, rollback, verificación y expiración persistente.
- Incluye política local de dominios exactos, destinos IPv4 y puertos, acceso público o
  privado por CIDR y HTTPS público obligatorio; TLS verificado a upstreams HTTPS.
- No incluye servidor MCP HTTP/OAuth, UI nueva, configuración arbitraria de Caddy,
  administración de contenedores/firewall, cambios DNS implícitos, despliegue real ni merge.

## Decisiones previas

- API y MCP comparten catálogo tipado y servicio autorizado. MCP stdio delega por HTTPS
  con token por cliente (hash en SQLite); HTTP solo en loopback. Sin cookies web ni token
  del Agent. Alta/revocación por CLI; el cliente nunca modifica sus permisos.
- Permisos exactos por acción, Agent, dominio, dispositivo, destino y puerto. Sin comodines
  de autorización. Consultas filtradas antes de paginar y sin cuerpos libres/secretos.
- SDK oficial MCP 1.x para stdio, sin implementar JSON-RPC manualmente.
- Caddy usa un bloque `dyndnsarr` aprovisionado manualmente en un servidor HTTPS existente;
  GET + PATCH con ETag/If-Match. Conserva rutas ajenas y rechaza conflictos de host.
  La API de Caddy valida y aplica atómicamente; se guarda historial propio para rollback.
- Intención persistida antes de red, operaciones serializadas e idempotentes; ante timeout
  se reconcilia sin afirmar éxito. Caducidad se comprueba también al recuperar tras reinicio;
  una caída del Hub/Caddy puede retrasar la retirada y debe documentarse.
- Destinos IPv4 literales configurados localmente: sin DNS rebinding ni URLs suministradas
  por herramientas. Bloqueo de metadatos y redirecciones; errores sanitizados.

## Plan de implementación

1. Contratos/política, migración Hub, identidad/hash/revocación y CLI; probar rechazo de
   permisos y ausencia de secretos.
2. Comandos DNS por registro y servicio compartido de consultas/acciones; probar que un
   registro fuera del permiso y `monitored` no se escriben, así como reintentos idempotentes.
3. Adaptador Caddy, historial/revisiones, expiración y recuperación; probar conflicto,
   rollback, 412, fallo inválido, respuesta perdida y conservación de rutas ajenas.
4. API autenticada con cuerpo acotado y puente MCP stdio; prueba real con cliente SDK.
5. Ejemplos, Compose, operaciones, wheels; lint/formato, suite y CI del PR.

## Criterios de aceptación

1. Integraciones desactivadas por defecto; v1.0 y landing conservan su comportamiento.
2. Token inválido/revocado rechaza antes del cuerpo. Cookies/credenciales de Agent no sirven.
3. Cliente de lectura no ejecuta operaciones ni ve recursos ajenos; denegaciones auditadas.
4. Consulta privada sin secretos ni payloads libres; no altera proyección pública.
5. DNS dirigido actúa solo sobre el registro autorizado y nunca escribe un `monitored`.
6. Repetir un ID con mismo contenido no duplica efectos; otro contenido devuelve conflicto.
7. Publicación solo con dominio, destino y puerto permitidos por política y cliente.
8. Config inválida/conflicto/412 conserva publicaciones y rutas ajenas.
9. Timeout/reinicio se reconcilia; publicación caducada no se reactiva al recuperar.
10. Rollback recupera una revisión válida aplicando permisos/política actuales.
11. Verificación distingue configuración aplicada de accesibilidad observada y no sigue redirects.
12. MCP inicializa, lista herramientas y consulta/opera usando el SDK oficial por stdio.
13. Cada mutación registra actor, acción, recurso, instante, resultado/código sin secretos.
14. Wheels incluyen módulos/migración; documentación reproduce alta, revocación, delegación
    MCP y aprovisionamiento Caddy manual sin cambiar el servidor del usuario.

## Pruebas

Tests unitarios de política/contratos y tests de integración API/Agent/Caddy simulado;
cliente MCP real sobre proceso stdio. `.venv/bin/ruff check .`,
`.venv/bin/ruff format --check .`, `.venv/bin/pytest`, `scripts/verify-wheels.sh`.
CI prueba Python 3.11/3.12 y Docker.

## Documentación a actualizar

ADR-0012, índice de slices/decisiones, plan, ESTADO, convenciones y operaciones §11;
`.env.example`, Compose y política de ejemplo. Sin modificar `.env` real.

## Riesgos y migraciones

Migración Hub 0008 aditiva, backup SQLite antes de actualizar. Tokens solo visibles al
emitirlos. Proteger API Caddy con red/HTTPS/proxy autenticado; no exponer su admin API.
El Hub debe continuar activo para retirar publicaciones temporales; reconciliación tras
caídas, sin prometer retirada durante indisponibilidad. `caddy run --resume` conserva cambios
API; recargar un Caddyfile estático puede perderlos. Actualizar Agent para DNS dirigido.

## Evidencia

- Base `main` actualizada con `origin/main`; PR #9 fusionado el 2026-10-20,
  commit `11e614888975ac3ad55180eed9ea3159aaa42d7d`.
- Referencias oficiales consultadas: [SDK MCP](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x),
  [API Caddy](https://caddyserver.com/docs/api). Evidencia final pendiente.
