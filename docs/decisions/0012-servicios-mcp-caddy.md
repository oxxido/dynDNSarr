# ADR-0012 — Servicios autorizados, MCP stdio y publicaciones Caddy

- Estado: aceptada
- Fecha: 2026-10-20
- Alcance: slice7, arquitectura §3, §5–6, §11–12; PRD §11–14 y §19.

## Contexto

MCP necesita identidad propia, operaciones limitadas y auditoría; una cookie de administrador
no es una delegación. Una actualización global DDNS ampliaría un permiso sobre un solo dominio.
La administración del proxy debe conservar las rutas existentes y recuperarse tras respuestas
perdidas. No hay autorización para cambiar el servidor real ni exponer su admin API.

## Decisión

API `/service/v1`: catálogo tipado en `service_models`, autorización en `service_api`, persistencia
separada en `services_repo`. Reutiliza el envío/reconciliación de `services/control` y la política
local del Agent. `check_dns`/`sync_ddns` admiten `resource_id` como nombre exacto, validado al
recibir y ejecutar; sin él se conserva el alcance completo de v1.0 para consola/UI.

Tokens de cliente `dds_`, aleatorios de 256 bits, hash SHA-256 en SQLite; alta y revocación
exclusivamente CLI. Permisos exactos por herramienta, Agent, dominio, dispositivo, destino y
puerto. No hay comodines ni permisos implícitos. La revocación bloquea solicitudes posteriores;
las intenciones ya aceptadas siguen su reconciliación y las publicaciones existentes permanecen
hasta su retirada o caducidad. Se auditan solicitud, rechazo y resultado sin cuerpos ni tokens.

MCP es un puente stdio con el SDK oficial Python 1.x mantenido (`>=1.28,<2`), con protocolo
implementado por el SDK. Se mantiene esta línea acotada para usar su servidor low-level y probar
con cliente real; migrar a v2 es un cambio explícito futuro. El puente consume la misma API con
TLS verificado, sin SQLite ni puerto propio. Evita introducir un segundo servidor HTTP y OAuth.

Caddy: política TOML local y bloque reservado `@id=dyndnsarr` dentro de un servidor HTTPS
`:443`. Solo genera reverse_proxy para hosts exactos y destinos IPv4/puertos autorizados;
HTTPS upstream verifica certificados. Acceso privado por `remote_ip`/CIDR de conexión, sin
confiar en cabeceras del cliente. GET de servidores y PATCH de las rutas del servidor con
ETag/If-Match, preservando otras rutas. Conflictos de hosts y cambios concurrentes no se pisan.
Caddy valida cada modificación y conserva la configuración previa si rechaza la nueva.

Intenciones antes de red, IDs UUID ligados a cliente/acción/contenido y revisión esperada.
Lock de proceso compartido por API y reconciliador, worker único y conexión SQLite propia;
ninguna transacción atraviesa un await. Respuestas ambiguas quedan pendientes: se vuelve a
leer Caddy y, si ya coincide, se confirma sin repetir PATCH. Historial propio permite rollback
con política/permisos vigentes, nunca restauración ciega de toda la configuración de Caddy.
Reconciliación cada 15 s y al arrancar: expira, deshabilita destinos retirados de la política y
repara deriva dentro del bloque reservado. No sustituye rutas ajenas.

## Consecuencias

Actualizar el Agent para DDNS dirigido; un Agent antiguo rechaza la operación sin ampliarla.
Caddy admin exige aprovisionamiento externo protegido y persistencia con `--resume`; no se
abre, instala ni modifica desde esta implementación. Certificados públicos/ACME los gestiona
Caddy, no DynDNSarr. Una modificación aplicada no garantiza accesibilidad: las verificaciones
miden el upstream y marcan explícitamente el HTTPS público como desconocido.

Caducidad retrasada mientras Hub o Caddy estén caídos; no sirve como barrera temporal de
seguridad estricta. El operador mantiene backups de la BD y de Caddy; revisiones/intenciones
conservan deduplicación y rollback, sin purga automática. El acceso privado por CIDR requiere
que Caddy vea la IP de conexión del visitante. No introduce autenticación de usuario por sitio,
DNS implícito, procesos de sistema ni cambios en firewall/servidor.

Referencias: [SDK MCP 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x),
[API Caddy y concurrencia](https://caddyserver.com/docs/api).
