# DynDNSarr — Technical Handoff

**Versión de documento:** TDD / Handoff v0.6 (borrador de implementación)  
**Fecha:** 2026-10-14  
**Estado:** listo para iniciar desarrollo por etapas. Los contratos propuestos abajo deben validarse con pruebas antes de fijarlos como API estable.  
**Documento fuente:** PRD funcional v0.3. **Arquitectura aprobada:** Agent \+ Hub v0.4. **Referencia visual:** landing pública HTML aprobada, con CSS/JS incrustados y PNG externos.

## 0\. Objetivo y reglas para el agente de programación

Construir DynDNSarr: un administrador DDNS para homelabs con **Agent autónomo en Raspberry Pi** y **Hub web en un servidor Docker administrado mediante Dockge**, ambos inicialmente en la misma LAN. Prioridades: fiabilidad DDNS, muy pocos recursos, simplicidad de instalación, seguridad y código Python mantenible.

**Reglas de implementación:**

1. No sustituir el stack ni agregar Redis, PostgreSQL, MQTT, Celery, React, Node.js o microservicios sin justificar una necesidad real.  
2. El **Agent nunca debe depender del Hub para realizar DDNS**. Pi-hole, Homarr, correo y web son opcionales.  
3. El **Hub no modifica DNS directamente**: pide al Agent operaciones autorizadas.  
4. No conectar la vista pública a esquemas internos ni devolver IP completa, MAC, nombres de dispositivos o secretos. Crear un DTO público específico.  
5. No copiar datos falsos de la maqueta a producción: todos los indicadores operativos provienen de observaciones con marca temporal o indican `unknown`.  
6. Crear pruebas automatizadas antes de integrar con DonDominio real o de exponer endpoints administrativos.  
7. Preservar la landing visual validada. Desde slice4 la mascota lleva una cara SVG superpuesta y animada (ojos que siguen al puntero, parpadeo, boca por estado), sombra, flotación y burbujas de fondo ([ADR-0008](decisions/0008-animacion-mascota.md)); los PNG no se alteran ni se reconstruye la mascota con SVG.  
8. Para cada etapa: código, tests, configuración de ejemplo, instrucciones de arranque, migraciones y criterios de aceptación demostrables.

## 1\. Alcance: v1.0 y posteriores

**v1.0**: proveedor DonDominio/DonDNS, múltiples dominios y registros `managed` o `monitored`, detección IPv4, actualización y verificación DNS, historial/uptime observado, Agent↔Hub, vista pública, login y dashboard privado, Pi-hole opcional, dispositivos, Wake-on-LAN manual/automático, correo/webhooks/Slack, integración de estado para Homarr, auditoría, exportación de diagnóstico. **No implementar IPv6 inicialmente**; si la landing de muestra menciona IPv6, sustituir por “No disponible” u ocultarlo.

**v1.1**: MCP con permisos `scoped` y auditoría, publicación de HTTP/HTTPS vía Caddy con validación/reversión/caducidad y ámbitos por dominio, destino y puerto. Diseñar interfaces preparatorias ahora; no implementar v1.1 durante los primeros incrementos.

**Fuera de alcance**: DHCP propio, reemplazar Pi-hole o Caddy, desplegador general, gestor de contenedores, firewall, VPN o control remoto irrestricto.

## 2\. Stack cerrado

| Área | Tecnología |
| :---- | :---- |
| Lenguaje | Python 3.11+ (la Raspberry destino trae 3.11.2; ver ADR-0002) |
| Agent | Servicio Python nativo `systemd` \+ servidor HTTP ligero FastAPI/Uvicorn |
| Hub | FastAPI \+ Uvicorn en **un contenedor** Docker Compose/Dockge |
| Plantillas | Jinja2 server-rendered, HTMX solo donde aporte valor; CSS/JS simple |
| Base de datos | SQLite **local e independiente** por componente; WAL y migraciones |
| HTTP Agent–Hub | REST/JSON **bidireccional basado en eventos**, TLS autenticado, sin polling de sincronización constante |
| Historial y logs | Eventos SQLite y `logging` estructurado sin secretos |
| Empaquetado | Agent: `venv` \+ wheel \+ `systemd`; Hub: imagen Docker multiarch cuando se publique |
| UI | Tema `system` (predeterminado), `light`, `dark`; persistir preferencia |

**Distinción esencial:** no hay polling de Agent→Hub cada 15 o 60 segundos para replicar datos. **Sí** hay comprobaciones locales periódicas de IP/DNS en Agent, reintentos de entrega tras fallos y un **health check opcional, independiente** de Hub→Agent para detectar Agent caído. Un dashboard abierto NO acelera automáticamente el motor DDNS.

## 3\. Topología y reglas operativas

Internet / navegador

       |

  \[Hub: Docker \+ Dockge, en servidor LAN\]

   \- HTML público / privado y autenticación

   \- configuración, alertas y auditoría

   \- SQLite Hub

       |  REST HTTPS autenticado, LAN

       v  (comandos)

  \[Agent: Raspberry Pi \+ systemd\]

   \- IP pública y DDNS DonDominio

   \- diagnóstico DNS, Pi-hole y Wake-on-LAN

   \- SQLite Agent \+ outbox persistente

       |  REST HTTPS autenticado, LAN

       ^  (eventos/resultados)

   \- DonDominio, resolvers externos, Pi-hole local

- Agent mantiene **última configuración válida** y credenciales necesarias en su equipo.  
- Hub es autoridad para la configuración de uso normal; aplica propuestas versionadas. Agent valida atómicamente, conserva configuración anterior ante fallo y confirma resultado.  
- Agent NO publica una API a Internet. Limitar interfaz de escucha y firewall de LAN para que la API privada sea accesible solo desde IP/identidad del Hub. Nunca asumir que “LAN \= autenticado”.  
- Para la primera instalación, configurar endpoints/identidades y TLS confiable; **mDNS puede venir después**, no es requisito inicial.  
- HTTPS entre ambos; evitar tokens reutilizables en HTTP plano. Para el primer prototipo local, preparar certificados CA local o certificados confiables vía reverse proxy; documentar trust store y rotación. **No usar `verify=False`**.  
- Hub expone la página pública (opcional) y la administración autenticada; montar reverse proxy HTTPS para acceso externo, con rate limiting donde corresponda.  
- El Hub puede quedar offline y el Agent debe seguir funcionando, persistir eventos y sincronizarlos después.  
- Las alertas ejecutadas solo por Hub no se entregarán mientras éste esté offline. El PRD admite explícitamente esta limitación. Decidir posteriormente si hay un canal crítico de Agent directo.

## 4\. Estructura de repositorio propuesta

dyndnsarr/

├── README.md

├── pyproject.toml

├── .env.example

├── packages/

│   └── shared/                  \# schemas, enums y contratos sin secretos

├── apps/

│   ├── agent/

│   │   ├── dyndnsarr\_agent/

│   │   │   ├── main.py          \# arranque, lifespan y API

│   │   │   ├── config.py

│   │   │   ├── db/              \# models, repositorios, migraciones

│   │   │   ├── ddns/            \# IP sources, DonDominio adapter, reconciler

│   │   │   ├── dns/             \# resolvers y verificación

│   │   │   ├── monitoring/

│   │   │   ├── integrations/    \# Pi-hole, WoL

│   │   │   ├── outbox/          \# eventos y entrega al Hub

│   │   │   ├── api/             \# API interna Agent

│   │   │   └── security/

│   │   └── systemd/dyndnsarr-agent.service

│   └── hub/

│       ├── dyndnsarr\_hub/

│       │   ├── main.py

│       │   ├── config.py

│       │   ├── db/

│       │   ├── services/        \# comandos/configuración/eventos

│       │   ├── api/             \# pública, admin y eventos Agent

│       │   ├── auth/

│       │   ├── notifications/

│       │   ├── integrations/    \# Homarr

│       │   ├── templates/       \# Jinja2

│       │   └── static/assets/   \# PNG originales, externos

│       ├── Dockerfile

│       └── compose.yaml

├── migrations/

├── tests/

│   ├── unit/

│   ├── integration/

│   ├── contract/

│   └── e2e/

└── docs/

    ├── prd-v0.3.md

    ├── architecture.md

    ├── ui-ux.md

    └── operations.md

Se puede simplificar la distribución exacta de Python packages, pero **no mezclar infraestructura HTTP, reglas DDNS y acceso SQLite dentro de un único archivo**. Compartir DTOs mediante modelos Pydantic versionados. Evitar un ORM pesado si `sqlite3` con repositorios y migraciones explícitas resulta suficiente. **Un solo worker** por proceso inicialmente: evitar schedulers duplicados.

## 5\. Contrato Agent ↔ Hub (propuesta inicial, no API ya aprobada)

Comunicación sin polling de sincronización constante:

### Agent → Hub

- `POST /api/v1/agents/{agent_id}/events`: lote de eventos almacenados en outbox.  
- `POST /api/v1/agents/{agent_id}/command-results`: confirma resultado de una operación.  
- `POST /api/v1/agents/{agent_id}/config-ack`: confirma/rechaza una nueva versión de configuración.

### Hub → Agent

- `GET /internal/v1/health`: respuesta mínima `agent_id`, `version`, `status`, `time_utc`, `last_local_dns_check_at` (solo admins autorizados).  
- `GET /internal/v1/status`: estado operativo actual, con observaciones y timestamps.  
- `PUT /internal/v1/config`: aplicar configuración versionada, con resultado `applied/rejected`.  
- `POST /internal/v1/commands`: acciones limitadas, p.ej. `check_dns`, `sync_ddns`, `wake_device`.

**En ambos extremos:** HTTPS, autenticación de servicio, validación y autorización por comando/recurso, límites de payload, timeouts, logs de auditoría, versionado API, códigos de error coherentes, identificadores de operación e idempotencia. No aceptar comandos arbitrarios del sistema ni URLs/destinos no autorizados.

### Esquemas mínimos

{

  "event\_id": "uuid",

  "agent\_id": "raspberry-home",

  "sequence": 101,

  "type": "dns\_update\_succeeded",

  "occurred\_at": "2026-10-14T15:00:00Z",

  "payload": {"record\_id": "record-1", "result": "verified"}

}

{

  "command\_id": "uuid",

  "type": "wake\_device",

  "resource\_id": "nas-1",

  "requested\_by": "admin:1",

  "expires\_at": "2026-10-14T15:05:00Z",

  "parameters": {}

}

- **At-least-once delivery de eventos:** transacción local registra `event_id` único en outbox; Hub hace upsert/deduplicación por `(agent_id,event_id)`; responde ACK después de commit; Agent elimina o marca como entregado solo tras ACK. Reintentos con jitter/backoff y límite razonable de almacenamiento.  
- **Comandos:** `command_id` de deduplicación; Hub puede consultar resultado por ID. Agent guarda primero recepción/estado, impide ejecutar dos veces un comando ya ejecutado y responde incluso ante timeout de la respuesta. WoL es especialmente sensible a reintentos: no reenviar automáticamente sin comprobar política y estado.  
- **Orden de eventos:** conservar `occurred_at` en UTC más `sequence` monotónico por agente; mostrar la diferencia entre el momento observado y el recibido.  
- **Configuración:** `config_version` creciente \+ hash de contenido; aplicar y confirmar o mantener versión anterior; nunca escribir secretos en eventos.  
- **Errores:** `type`, `code`, `message_safe`, `timestamp`, `request_id`; detalles internos solo en logs privados sanitizados.

## 6\. Modelos de datos mínimos

### Agent SQLite

- `agent_settings`, `configuration_versions`, `dns_records` (config activa), `public_ip_observations`, `dns_checks`, `dns_update_attempts`.  
- `incidents` (start/end/unknown gaps), `outbox_events` (id, seq, type, timestamp, payload, sent/acked, retry), `commands` (idempotencia), `device_policies` (WoL), `integration_health`.  
- Secretos: no escribir texto plano en dumps/exportaciones y nunca en logs. Proteger archivo de configuración y directorio mediante propietario/permisos mínimos. Diseñar rotación de credenciales.

### Hub SQLite

- `users`, `sessions` o storage de sesión seguro, `agents`, `agent_credentials`, `desired_config_versions`, `observed_agent_state`, `events`, `dns_records` (vistas materializadas/caché), `incidents`, `alerts`, `delivery_attempts`, `audit_log`, `user_preferences`.  
- No almacenar contraseñas recuperables: hash seguro (Argon2id o equivalente) y cookies de sesión HttpOnly, Secure, SameSite.  
- Backups de SQLite consistentes (backup API o snapshot coordinado; no copiar solo `.db` durante WAL activo). Migraciones versionadas y test de recuperación tras power-loss.

## 7\. Motor DDNS Agent (prioridad de fiabilidad)

1. En inicio: cargar última configuración válida, abrir SQLite y recuperar operaciones pendientes; ejecutar comprobación de conectividad/IP y verificación DDNS.  
2. Consultar IPv4 pública desde **dos proveedores configurables** y tratar resultados contradictorios como estado incierto (no cambiar DNS inmediatamente).  
3. Obtener IP objetivo y estado DNS del registro. Comparar IP pública observada con objetivo, evitar cambios innecesarios.  
4. Para `managed`, llamar a adaptador DonDominio/DonDNS cuando corresponda; para `monitored`, **no** emitir escrituras.  
5. Reintentos limitados con backoff y manejo de rate limits; evitar solicitudes repetidas y falsos positivos.  
6. **Distinguir**: solicitud enviada → proveedor aceptó → DNS realmente resuelve correctamente (con verificaciones contra resolvers externos).  
7. Emitir eventos solo para cambios/alertas/resultados relevantes; guardar muestras locales de observación necesarias para uptime y diagnósticos, con retención limitada.  
8. Si Pi-hole falla, no bloquear DDNS. Resolvers externos no deben depender exclusivamente de Pi-hole.

**No inventar datos del API DonDominio**: antes de codificar el adaptador, verificar documentación oficial vigente y construir pruebas simuladas y pruebas controladas con credenciales de desarrollo. No registrar claves.

## 8\. Monitorización, estados y uptime

Estados: `healthy`, `degraded`, `critical`, `unknown`. Deben asignarse según reglas parametrizables, usando timestamps, umbrales e información observada.

- `healthy`: evidencias suficientes y verificaciones consistentes.  
- `degraded`: inconsistencia o fallos transitorios.  
- `critical`: fallo persistente confirmado.  
- `unknown`: no hay información reciente o el Agent no pudo observar.

Calcular uptime **observado**, sin afirmar 100% sobre periodos en que no hubo observaciones. Conservar intervalos de indisponibilidad desconocida. Períodos UI: últimas 24 horas, 7 días y 30 días.

Un Agent inalcanzable **no significa necesariamente** que Internet de la vivienda haya caído. Hub debe reflejar `agent_unreachable` y última observación, sin fingir diagnóstico de Internet remoto.

## 9\. Vista pública y privada (referencia gráfica real)

**Fuente visual validada:** `dyndnsarr-landing-inline.zip`, estructura: `index.html` (CSS y JS incrustados) \+ `assets/dyno-green.png`, `dyno-orange.png`, `dyno-red.png` externos. **Ojos y boca animados** ([ADR-0008](decisions/0008-animacion-mascota.md)): cara SVG superpuesta a los PNG sin rostro. Se mantienen diseño, mascota (PNG originales con cara, sombra, flotación y burbujas de fondo), responsive y selector `Sistema / Claro / Oscuro`. La landing actual contiene **datos simulados** y un selector de estado de desarrollo que se debe retirar/ocultar en producción.

**Importante:** Los PNG de la referencia actual están **sin ojos ni boca**; la cara es un SVG superpuesto con coordenadas sobre 1254×1254 ([ADR-0008](decisions/0008-animacion-mascota.md)). Si se sustituyen los PNG por otros con otra proporción o posición del cuerpo, hay que reajustar la cara (macro `face` de la plantilla).

**UI pública**: solo estado general, mascota, última comprobación exitosa y hasta cinco renovaciones, con fechas y los **dos últimos dígitos de IPv4** cuando corresponda. No exponer dominio concreto si su nombre facilita información privada, salvo que el producto/administrador lo autorice explícitamente; el PRD autoriza renovaciones pero restringe infraestructura y se debe usar política conservadora de nombres mostrados. No mostrar IP completa, dispositivos, MACs, configuración, tokens, usuario administrador, logging interno. Incluir opción para desactivar página pública.

**Reconciliar maqueta con PRD:** la maqueta puede contener IPs/dominios de ejemplo, estados IPv6 y historial ficticio. Producción debe poblar datos permitidos desde un `PublicStatusDTO` sin campos sensibles; ocultar o anonimizar cualquier elemento no permitido. Se puede conservar la misma estructura y aspecto visual con datos seguros.

**UI privada**: login, estado, dominios, historial, diagnósticos, Pi-hole/dispositivos, WoL, alertas/configuración. Formularios POST/PUT con CSRF/antiforgery (si usan cookies), validación de entrada y auditoría. Controles de aplicación sensibles no deben dispararse desde GET.

**Actualización navegador–Hub:** HTML/Jinja2, HTMX bajo demanda. SSE es una mejora posible para eventos sin refrescos; **no es requisito de la primera iteración**. Si se implementa, autorizar la conexión y no filtrar datos internos al canal público.

Tema `system/light/dark`: `prefers-color-scheme`, selector manual, preferencia persistida, sin destello de tema erróneo. Accesibilidad y `prefers-reduced-motion`.

## 10\. Integraciones v1.0

### Pi-hole

- Solo lectura del dispositivo/hostname/IP/MAC (privado), concesiones DHCP, actividad y salud de Pi-hole.  
- El estado de una concesión DHCP no equivale a “dispositivo conectado”. Diferenciar `active`, `likely_active`, `known`, `stale`, `unknown` según evidencias.  
- Error de Pi-hole no bloquea DDNS.

### Wake-on-LAN

- Política por dispositivo: MAC, IP esperada, permitido manual/automático.  
- Acciones: `sent`, `booting`, `reachable`, `unconfirmed`.  
- Auto-WoL solo tras recuperación del agente y solo para dispositivos expresamente autorizados; protección anti-loop y repetición. No apagar ni forzar reinicios.

### Alertas

- Email SMTP y webhooks HTTP/HTTPS, payload compatible con Slack; sin Telegram inicial.  
- Umbrales, severity, supresión de duplicados, reintentos, alertas de recuperación, botón de prueba y auditoría. Proteger destinos contra SSRF, incluyendo IPs locales sensibles salvo allowlist explícita.

### Homarr

- Estado resumido, última comprobación, último cambio IP, URL de aplicación. La integración es opcional; autenticar si contiene información administrativa.

## 11\. Seguridad y despliegue

**Agent Raspberry**:

- Paquete Python/wheel, `venv` aislado, usuario Linux dedicado, `systemd` `Restart=always` y límites de recursos apropiados; rutas sugeridas `/etc/dyndnsarr/` (config/secretos) y `/var/lib/dyndnsarr/` (SQLite).  
- Comandos de utilidad propuestos: `dyndnsarr-agent status`, `doctor`, `check-ip`, `check-dns`, `sync-ddns` (los nombres son parte del diseño, no existen todavía).  
- Interfaz Agent escuchando solo en IP LAN autorizada, firewall restringiendo origen, tokens revocables y TLS verificado. Para WoL, conceder capacidades únicamente cuando realmente se necesiten.  
- No almacenar secretos en repositorio o variables mostradas en logs; proteger backups.

**Hub Dockge**:

- Un servicio Docker Compose, volumen `/data` persistente, Uvicorn con un worker inicialmente, healthcheck, `restart: unless-stopped`, contenedor sin root si es viable, puerto expuesto preferentemente solo LAN y HTTPS mediante Caddy externo si ya existe.  
- No fijar imágenes que aún no fueron publicadas. Publicar tags semánticos una vez exista CI.  
- Separar secretos de variables visibles y documentar backup/restore de `/data`.

**Paring/emparejamiento**:

- MVP: aprovisionamiento manual seguro desde Hub con token/certificado por agente y verificación de identidad; códigos de un solo uso o mDNS de onboarding quedan como mejora. No exigir descubrimiento automático para completar el primer corte funcional.
- Fase 7 (slice8, [ADR-0013](decisions/0013-instalacion-emparejamiento.md)): emparejamiento por código compartido de un solo uso (≥128 bits, HMAC, sin que el código viaje), puerto de enlace HTTPS propio del Hub con su CA y CA del Agent fijada por agente. El aprovisionamiento manual sigue disponible; mDNS sigue fuera.

## 12\. Hoja de ruta de implementación con entregables verificables

### Fase 0 — Monorepo y entorno (primer PR)

- Crear estructura, `pyproject.toml`, lint/format, `pytest`, docs, CI básica, ejemplos `.env`, contenedores de desarrollo.  
- Arranque Agent y Hub con `/healthz`, SQLite/migraciones y logging seguro.  
- Verificación: ambos arrancan y vuelven a arrancar con datos persistidos; sin secrets en logs.

### Fase 1 — Motor DDNS standalone (primer MVP real)

- Agent sin Hub, fuentes IPv4 y reconciliador; adaptador DonDominio; registro `managed`/`monitored`, verificaciones de DNS, eventos/auditoría SQLite, scheduler independiente.  
- Tests de cambios, conflictos de fuentes, caídas del proveedor, reinicios, rate limit y recuperación.  
- **Aceptación:** cambia IP simulada → actualiza registro administrado una sola vez → verifica DNS; con Hub apagado sigue funcionando.

### Fase 2 — Enlace seguro Hub–Agent

- Certificados/tokens, endpoints de comando/estado/eventos, outbox at-least-once, ACK/idempotencia, configuración versionada.  
- **Aceptación:** detener Hub, generar eventos, arrancarlo, recibir todos una sola vez en Hub; aplicar config inválida no cambia el Agent.

### Fase 3 — Landing pública real

- Integrar `index.html` aprobado a Jinja2 conservando CSS/JS inline e imágenes estáticas en `static/assets`.  
- Sustituir estado simulado por `PublicStatusDTO`; tema claro/oscuro/sistema y opciones de publicación.  
- **Aceptación:** inspección de HTML, DOM, JSON y endpoints públicos demuestra ausencia de IP completa y datos privados.

### Fase 4 — Hub privado y diagnósticos

- Login seguro, sesiones, dashboard, múltiples registros y proveedores conceptuales, historial/uptime observado, diagnósticos, UI configuración y acciones con autorización/CSRF.  
- **Aceptación:** operaciones privadas inaccesibles sin login; dominio `monitored` nunca se escribe.

### Fase 5 — Integraciones, alertas y release v1.0

- Pi-hole read-only, WoL manual/auto, SMTP/webhooks/Slack, Homarr, backoff, retención, exportación, documentar instalación en Raspberry y Dockge.  
- **Aceptación:** integraciones caídas no bloquean DDNS; instalaciones limpias reproducibles; recuperación postreinicio.

### Fase 6 — v1.1 (posterior)

- API de servicios compartida, servidor MCP con scopes y auditoría, adaptador Caddy con validación, expiración, rollback y controles anti-SSRF.

### Fase 7 — Instalación asistida y emparejamiento

- Instalador idempotente del Agent (systemd, TLS y tokens generados), `compose.yaml` pegable en Dockge, emparejamiento Agent ↔ Hub por código compartido, puerto de enlace HTTPS del Hub y setup web del primer administrador con token de un solo uso ([ADR-0013](decisions/0013-instalacion-emparejamiento.md)).
- **Aceptación:** clonar, rellenar el `.env` y un comando por máquina; el mismo código en los dos lados enlaza Agent y Hub en cualquier orden, sin copiar tokens ni CA; un Hub impostor no recibe tokens.

### Fase 8 — Caddy vía Agent

- El Agent de la Raspberry que ejecuta Caddy escribe un fragmento importado por el Caddyfile y recarga por `localhost:2019`; gateway guiado; autopublicación de landing y admin.
- **Aceptación:** recargar el Caddyfile no pierde publicaciones; la admin API nunca sale de la Raspberry.

### Fase 9 — UI de publicaciones

- Crear, editar, retirar y revertir publicaciones desde el admin web.
- **Aceptación:** todas las operaciones con CSRF y auditoría.

## 13\. Matriz de pruebas mínimas

1. IP pública sin cambio → ninguna renovación innecesaria.  
2. IP cambia y DonDominio falla temporalmente → backoff y recuperación; estado degrada correctamente.  
3. Dos detectores IP discrepan → no actualizar hasta confirmación.  
4. Proveedor aceptó pero DNS externo aún no propaga → “pendiente de verificación”, no “correcto”.  
5. Reinicio de Raspberry → config/credenciales y outbox persistentes; comprobación inmediata.  
6. Hub offline 24 horas → Agent continúa; eventos se almacenan dentro de límites y se sincronizan cuando vuelve.  
7. Evento duplicado → un solo evento lógico en Hub.  
8. Reenvío duplicado de WoL → no despertar repetidamente fuera de política.  
9. Configuración inválida desde Hub → Agent conserva última versión activa.  
10. Caída Pi-hole/SMTP/Homarr → DDNS continúa sin bloqueos.  
11. Navegador no autenticado → ninguna API administrativa ni datos privados.  
12. Vista pública → sin IPv4 completa, MAC, credenciales, dispositivos ni información interna.  
13. Switch Sistema/Claro/Oscuro → persiste y sigue cambios de SO en modo Sistema.  
14. Docker y systemd se reinician → datos y servicios recuperados.  
15. Preferencia de animación reducida → ninguna animación no esencial (cara, flotación, sombra y burbujas; las pupilas quedan centradas).

## 14\. Decisiones técnicas aún abiertas (resolver sin cambiar arquitectura)

- **A1**: API exacta de DonDominio, credenciales permitidas, límites y verificación de propagación (validar contra fuente oficial).  
- **A2**: TLS Agent–Hub: CA local/certificados gestionados por Caddy y procedimiento de rotación de tokens.  
- **A3**: Librería de scheduler y de acceso a SQLite; escoger opción mínima tras spike, sin worker extra.  
- **A4**: Política exacta de retención, frecuencia de comprobación IP y umbrales de estados (configurables; proponer defaults documentados).  
- **A5**: Cómo representar públicamente dominios/renovaciones sin revelar infraestructura (aprobar un DTO con ejemplos reales anonimizados).  
- **A6**: Registro/descubrimiento del Hub inicial: manual seguro ahora, mDNS opcional futuro.  
- **A7**: Asset facial estático final: los PNG aprobados actuales no incluyen ojos/boca. Definir si la landing v1 usa las bases sin rostro o recibe sprites completos antes del release. **Resuelta en slice4:** bases sin rostro con cara SVG superpuesta ([ADR-0008](decisions/0008-animacion-mascota.md)).

## 15\. Prompt de arranque para Codex/Claude

> Implementá **solo Fase 0** de DynDNSarr, siguiendo este handoff y el PRD v0.3. Usa Python 3.12+, un monorepo con `agent`, `hub` y modelos compartidos. Crea FastAPI mínima en ambos, configuración validada, SQLite con migraciones, endpoints `/healthz`, pruebas unitarias y una guía reproducible de arranque local, systemd y Docker Compose para Dockge. No implementes todavía DDNS real, páginas con datos simulados, integraciones ni MCP/Caddy. Mantén independencia operativa del Agent y prepara contratos versionados. Antes de programar, enumera las decisiones abiertas que afectan esta fase; después trabaja en pasos pequeños y corre tests. No modifiques la landing aprobada.

---

**Definición de listo para empezar:** repositorio creado, PRD adjunto, ZIP de landing disponible como referencia, decisiones A1/A2 identificadas para las fases siguientes y fases implementadas secuencialmente con pruebas. Este documento es un handoff implementable, no una garantía de que el código o los servicios ya existan.