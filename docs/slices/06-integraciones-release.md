# Slice 6 — Integraciones y release v1.0

- **Estado:** fusionado
- **Fase (arquitectura):** 5 (§10, §11, §12 y §13 casos 8, 10–12, 14)
- **Rama:** `slice/06-integraciones-release`
- **PR:** [#9](https://github.com/oxxido/dynDNSarr/pull/9)

## Objetivo

Completar v1.0 con integraciones opcionales que no condicionen el DDNS: inventario Pi-hole,
WoL por política local, alertas persistentes SMTP/webhook, resumen Homarr, retención y exportación.

## Alcance

- Incluye Pi-hole v6 de solo lectura en el Agent, dispositivos y concesiones con evidencia temporal;
  WoL manual/automático tras reinicio del Agent; vistas privadas, comandos idempotentes y auditoría.
- Alertas en el Hub por eventos, severidad, espera mínima, supresión, recuperación y reintentos;
  SMTP TLS y webhooks compatibles con Slack. Botón de prueba. Configuración local TOML sin secretos.
- Homarr mediante widget iframe público limitado a su origen configurado y JSON sin datos privados;
  retención configurable del Hub, exportación privada por lista blanca, wheels y receta de release.
- No incluye Pi-hole v5, administración de Pi-hole, apagado/reinicio de equipos, ICMP privilegiado,
  MCP, administración Caddy ni cambios en el servidor, `.env` real o servicios reales.

## Decisiones previas

- Base: `main` actualizado, árbol limpio, PR #7 y #8 fusionados. Interpretación de «slide 6»: slice6.
- Pi-hole v6 confirmado por el usuario durante la implementación.
  Contrato oficial: [API](https://docs.pi-hole.net/api/) y especificaciones FTL
  [auth](https://github.com/pi-hole/FTL/blob/master/src/api/docs/content/specs/auth.yaml),
  [network](https://github.com/pi-hole/FTL/blob/master/src/api/docs/content/specs/network.yaml),
  [DHCP](https://github.com/pi-hole/FTL/blob/master/src/api/docs/content/specs/dhcp.yaml).
- Agent: archivo local de integraciones separado de la configuración DDNS remota, secretos por entorno,
  tarea/cliente/conexión SQLite propios. Los GET privados solo leen snapshots; timeout/backoff por integración.
- DHCP solo acredita un dispositivo conocido. Consulta DNS reciente → `likely_active`, TCP a un puerto
  expresamente configurado → `active`; evidencia vencida → `stale`, fallo de integración → `unknown`.
- WoL: destinos MAC/IP/broadcast/puerto fijados en política local; sin destinos del navegador. Reservar
  intento antes de UDP, cooldown persistido y comprobación TCP previa; auto solo después de un arranque
  posterior al primero, explícito por dispositivo. No repetir al reiniciar durante cooldown ni ante resultado incierto.
- Alertas: entrega at-least-once, IDs estables (el receptor puede deduplicar), cola y backoff persistidos.
  Payloads sin IP/MAC/token/cuerpo original. HTTPS/SMTP TLS verificados; redirects deshabilitados,
  DNS resuelto y fijado por petición, redes locales solo con allowlist explícita, metadatos prohibidos.
- Homarr no requiere credenciales porque solo lee proyección pública; con publicación apagada también
  queda apagado. No abrir iframe del admin. [Widget oficial](https://homarr.dev/docs/widgets/iframe/).
- Release: versionado 1.0.0 y workflow para tags semánticos, sin crear tag/release antes del merge.

## Plan de implementación

1. Contratos/config estrictos y migraciones aditivas; pruebas de validación, destinos y upgrade.
2. Adaptador Pi-hole probado con respuestas oficiales y transporte falso; snapshots privados acotados.
3. WoL manual/auto con política, persistencia, cooldown y recuperación; pruebas UDP/TCP falsas.
4. Cola de alertas, reglas y canales con timeouts, reintentos, supresión y recuperación; pruebas de reinicio y SSRF.
5. Vistas privadas y acciones con sesión/CSRF/idempotencia existentes; Homarr público y exportación segura.
6. Retención, ejemplos, operaciones, versiones y CI/release; ejecutar suite, wheels y revisar diff.
7. Commits, push, PR contra main y revisión de CI. Sin merge ni instalación real durante este encargo.

## Criterios de aceptación

1. Pi-hole caído, auth inválida o respuesta malformada no detienen DDNS ni pierden último snapshot válido;
   lectura nunca modifica Pi-hole. Concesión DHCP por sí sola no muestra conexión confirmada.
2. Sin sesión o CSRF no se consultan dispositivos ni envían paquetes/alertas. Manipular destino o
   recurso falla; mismo command_id no envía dos paquetes, incluso tras reinicio. Cooldown limita IDs nuevos.
3. Auto-WoL solo para dispositivo autorizado tras reinicio, con comprobación previa y resultado
   `sent`, `booting`, `reachable` o `unconfirmed`, sin afirmar arranque por el envío UDP.
4. Reglas de alertas respetan eventos/severidad/umbral/supresión/recuperación; fallos se reintentan
   con backoff tras reiniciar; prueba auditada. SMTP/webhook caídos no bloquean DDNS ni ingestión.
5. Destinos SSRF/redirect/rebinding se rechazan; secretos no aparecen en cola, eventos, logs o exportación.
6. Homarr muestra estado, comprobación, último cambio IP y enlace, sin IP/MAC/nombres internos;
   sigue las opciones de publicación. La vista privada conserva CSP y no-store.
7. Purga configurable y acotada del Hub conserva pendientes de entrega/comandos activos/deduplicación vigente.
   Exportación privada acotada por lista blanca, sin payload arbitrario ni configuración/secretos.
8. Migraciones, reinicio y wheels conservan datos y políticas. Suite, lint y empaquetado correctos;
   Docker en CI. Instalación Raspberry/Dockge reproducible documentada y release por tag preparado.

## Pruebas

Tests unitarios de clasificación, políticas WoL, SSRF y reglas/retención; integración con HTTP/SMTP/
UDP/TCP falsos y Agent/Hub reales en proceso, auth/CSRF y canarios públicos. Regresión completa,
ruff y `scripts/verify-wheels.sh`; Docker mediante CI. Sin credenciales ni consultas de integración reales.

## Documentación a actualizar

Operaciones, ejemplos de integraciones y entorno, README/ESTADO, convenciones, ADR e índice de slices.

## Riesgos y migraciones

Migraciones aditivas Agent 0006 / Hub 0007+. Paquete mágico no confirma encendido. TCP fallido no
prueba apagado. SMTP/webhook puede duplicarse tras respuesta perdida; ID permite deduplicar.
Las observaciones y avisos empiezan al activar la integración, sin inventar historia previa.
La publicación del release y pruebas de hardware quedan para después del merge y despliegue del usuario.

## Evidencia

- 2026-10-20: planificación desde `1735a3a`, `main == origin/main`, árbol limpio; reglas y secciones
  afectadas revisadas, sin leer `docs/deprecated/` ni `.env`. APIs verificadas en fuentes oficiales.


**Implementación y validación local, 2026-10-20:**

- Commits de implementación separados: Agent/contratos/red segura y Hub/alertas/UI/retención.
- Pi-hole v6 confirmado por el usuario; lecturas oficiales con SID en memoria, muestras privadas,
  fallback ante fallo; WoL reservado antes de red, cooldown persistido y auto solo tras recuperación.
- Migraciones Agent 0006 / Hub 0007; resource_id persistido, señales/entregas/tombstones y hecho
  público del último cambio IP (sin IP). Alertas respetan secuencia para no reabrir una recuperación
  nueva por un fallo retrasado. No se expone payload arbitrario a avisos o exportación.
- Primera regresión rápida ampliada: **840 passed**. Suite completa inicial: **876 passed y 1 fallo**
  por expectativa antigua del E2E (wake_device ya existe y devuelve device_not_allowed sin política).
  Se actualizó esa expectativa: E2E afectado + alertas, **8 passed**. Última verificación dirigida
  de alertas/SMTP TLS real, rutas privadas/Homarr y acciones con Agent real: **27 passed**.
- `scripts/verify-wheels.sh`: wheels 1.0.0 construidos/instalados en venv limpio; healthchecks,
  login, siete vistas privadas, exportación, integraciones y widget/JSON Homarr correctos.
  Temporales y caché de pip del script confinados al repositorio.
- QA con Chrome/Playwright instalado: **12 capturas**, dispositivos/alertas a 390 y 1280 px,
  claro/oscuro/sistema, sin desbordamiento. Transporte ASGI de pruebas y datos sintéticos;
  TLS real HTTP/SMTP y proxy de procesos cubiertos en pruebas separadas.
- `ruff check`, `ruff format --check`, `bash -n scripts/verify-wheels.sh` y `git diff --check` correctos.
- Workflows de CI y release preparados. No se ejecuta Docker local ni se publica tag/imagen/release
  antes del merge. No se ha leído `.env`, `docs/deprecated/` ni se han cambiado AGENTS.md o el servidor.
- Comprobaciones del hardware/Pi-hole/SMTP/webhook/Homarr propios y despliegue Raspberry/Dockge
  pendientes del administrador, según operaciones §10.5; no se simula evidencia de instalación real.

**Entrega:** rama subida y [PR #9](https://github.com/oxxido/dynDNSarr/pull/9) abierto contra main,
sin merge. Suite completa final Python 3.11/3.12, wheels e imagen Docker: evidencia viva en
[los checks del PR](https://github.com/oxxido/dynDNSarr/pull/9/checks). La publicación de tag/release
sigue pendiente del merge y decisión del mantenedor; no equivale a este PR abierto.

- Merge del PR #9 verificado el 2026-10-20: `11e614888975ac3ad55180eed9ea3159aaa42d7d`.
