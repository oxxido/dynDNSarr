# ADR-0011 — Integraciones opcionales v1.0

- Estado: aceptada
- Fecha: 2026-10-20

## Contexto

Pi-hole y WoL necesitan acceso LAN; las alertas y Homarr pertenecen al Hub. Ninguno puede
condicionar la reconciliación DDNS ni ampliar los destinos operables desde el navegador.

## Decisión

- Integraciones del Agent en TOML local separado, secretos por entorno; tarea, cliente HTTP y
  conexión SQLite propios. Pi-hole v6 usa sesión en memoria y GET de inventario/concesiones.
  No se administran DNS/DHCP de Pi-hole. Su API oficial se documenta en
  [Pi-hole](https://docs.pi-hole.net/api/) y en las especificaciones FTL enlazadas por slice6.
- Los estados distinguen evidencia probable (DNS reciente), concesión conocida, dato antiguo,
  desconocido y accesibilidad TCP positiva. No inferir apagado por TCP fallido.
- WoL solo a políticas locales explícitas (MAC/IP/broadcast/puertos). Reservar en SQLite antes
  del primer await/UDP, cooldown persistido y comprobación TCP previa. Comando con resource_id
  persistido e idempotencia por ID; recuperación de comandos interrumpidos no los reejecuta.
  Auto-WoL solo en arranques posteriores al primero, por autorización individual y cooldown.
- Alertas por reglas locales del Hub, sin red durante la ingestión. Guardar señales y cola
  en la misma transacción que los eventos nuevos; umbrales desde la recepción, supresión por
  regla/Agent/registro, recuperación explícita y hasta ocho intentos con backoff 30–3600 s.
  Cola de 10 000 pendientes, entregas acotadas por pasada y auditoría con códigos seguros.
  Un evento puntual sin recovery_events no se convierte en una incidencia recurrente.
- TLS verificado en SMTP/webhooks/Pi-hole. Resolver el hostname, validar todas las direcciones
  y fijar la IP de conexión manteniendo Host/SNI; no redirects ni proxies del entorno.
  Redes locales solo con allowlist; multicast, unspecified, link-local y metadatos conocidos
  prohibidos. Nunca incluir URLs secretas ni payloads originales en avisos, colas o exportación.
- El widget Homarr usa exclusivamente proyección pública y solo admite iframe del origen
  configurado; admin conserva frame-ancestors none. PUBLIC_ENABLED=false apaga también Homarr.
- Retención Hub configurable, purga acotada y tombstones por ventana explícita; Agent conserva
  su política de retención existente. Exportación JSON privada paginada de metadatos permitidos.
- Versión 1.0.0 preparada; publicar wheels, checksums e imagen semántica mediante tag en main
  después de verificar pruebas/wheels/Docker. No crear tag ni release desde una rama del slice.

## Consecuencias

Pi-hole v5 no está soportado. TLS y WoL se prueban sin permisos privilegiados. Los cambios de
políticas/canales requieren reinicio; el TOML DDNS remoto no puede modificar estas secciones.
Una reserva previa al envío evita repetición pero deja resultado incierto si el proceso cae.
SMTP/webhook son at-least-once: una respuesta perdida puede duplicar el aviso con el mismo ID.
No se promete detectar o notificar cuando Agent/Hub están apagados, ni se reconstruye actividad
anterior a la recogida. La ventana de tombstones debe cubrir la retención de eventos del Agent.
