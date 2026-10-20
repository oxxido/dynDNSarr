# DynDNSarr 1.1.0 — AI & Gateway

API de servicios y MCP stdio con identidades revocables, permisos exactos y auditoría.
Consulta de conectividad, registros, historial, dispositivos y publicaciones; comandos DNS
por registro y WoL dentro de la política del Agent.

Publicaciones HTTPS Caddy con destinos autorizados, acceso público/privado por CIDR,
revisiones, idempotencia, rollback, caducidad y recuperación de respuestas perdidas.
No modifica DNS al publicar. Integraciones desactivadas por defecto.

Actualizar los tres wheels/imagen y el Agent para comandos por registro. Migración Hub 0008
aditiva; hacer backup SQLite antes de actualizar. La política Caddy y los permisos se
aprovisionan manualmente: [operaciones §11](operations.md#11-api-de-servicios-mcp-y-caddy-slice7).
No se publica un tag ni se modifica el proxy de producción al abrir el PR.

Límites: MCP stdio, sin MCP HTTP/OAuth; solo IPv4 literal y HTTPS público automático; el
acceso privado requiere IP de conexión directa a Caddy. La retirada temporal puede
retrasarse si Hub/Caddy están apagados. Verificación del upstream separada del estado
HTTPS público. No es un gestor general de Caddy ni una autenticación de usuario por sitio.
