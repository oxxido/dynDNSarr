# DynDNSarr 1.0.0

Agent autónomo DDNS para Raspberry (Python 3.11+) y Hub privado/público para Docker/Dockge.
Incluye DonDominio, monitorización DNS, enlace TLS autenticado y comandos idempotentes,
administración web, Pi-hole v6 de solo lectura, WoL manual/automático por política, alertas
SMTP/webhook compatibles con Slack y widget Homarr público sin información administrativa.

Las integraciones son opcionales. Los destinos WoL se fijan en el Agent y los canales/reglas
de alertas en el Hub; contraseñas/tokens/URLs secretas se reciben por entorno. El DDNS continúa
si el Hub o una integración no están disponibles.

Migraciones aditivas: Agent 0006 y Hub 0007. Hacer backup consistente antes de actualizar y
actualizar primero el Agent. Pi-hole v5 no está soportado. El paquete mágico no prueba que el
equipo haya arrancado; un TCP fallido tampoco demuestra que esté apagado. SMTP/webhook pueden
repetir un aviso si se pierde su respuesta; conservar el ID permite deduplicarlo.

Instalación y comprobación: `docs/operations.md`, secciones 2, 3 y 10. Los wheels y SHA256SUMS
se adjuntan al release y la imagen semántica se publica en GHCR cuando se crea un tag validado.
Este documento prepara el release; su existencia en una rama no implica que ya esté publicado.
