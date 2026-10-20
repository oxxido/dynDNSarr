# Slice 5 — Hub privado y diagnósticos

- **Estado:** fusionado
- **Fase (arquitectura):** 4 ([architecture.md](../architecture.md) §5, §6, §8, §9, §11, §12 Fase 4 y §13 casos 9, 11–13)
- **Rama:** `slice/05-hub-privado-diagnosticos`
- **PR:** [#7](https://github.com/oxxido/dynDNSarr/pull/7)

## Objetivo

El administrador inicia sesión en el Hub y consulta el estado real de sus Agents y registros, la IP completa, los diagnósticos DNS, el historial y el uptime observado de 24 h / 7 d / 30 d. Puede cambiar la configuración administrable y solicitar `check_dns` o `sync_ddns`, con autorización, CSRF, idempotencia y auditoría. El Hub sigue delegando el DDNS al Agent; los registros `monitored` nunca se escriben.

Referencias de producto: [requirements.md](../requirements.md) DNS-05–07, MON-01–03, LOG-01–02, WEB-02–03, API y CFG-01/03. Prevalece la Fase 4 de arquitectura: las integraciones, exportación y retención general del Hub pertenecen al slice 6.

## Alcance

**Incluye**

- Un administrador local, aprovisionado y recuperable por CLI; login, logout, contraseña con hash Argon2id, sesiones revocables y limitación persistente de intentos.
- Vistas Jinja2 bajo `/admin`: resumen, Agents/registros, historial, diagnósticos, configuración y seguimiento de comandos. Varios Agents y varios registros por Agent; identidad de registro = `(agent_id, name)`.
- Estado por registro y agregado, umbrales configurables (A4), métricas e intervalos de incidencias observadas con cobertura y huecos desconocidos.
- Historial de eventos existentes y auditoría administrativa, filtros y paginación. Separar observación, actualización aceptada y verificación DNS.
- Formularios de configuración del Agent para registros, frecuencia, fuentes IPv4, resolvers, verificación y retención local, conservando versión/hash/ack de 3b. Configuración de umbrales del Hub mediante UI.
- Diagnósticos desde observaciones ya persistidas en el Agent. Extensión aditiva del contrato privado para consultarlas y obtener la configuración efectiva administrable.
- API privada de respaldo bajo `/admin/api/v1`, con la misma sesión, autorización y CSRF que los formularios. Servicios de aplicación compartidos con la CLI donde ya existen operaciones equivalentes.
- Regresión de la separación pública/privada, migraciones, configuración de ejemplo, empaquetado y documentación de despliegue HTTPS.

**No incluye**

- Pi-hole, dispositivos, WoL, alertas, Homarr, exportación, política general de retención del Hub ni release (slice 6). MCP, tokens para terceros y Caddy administrado (slice 7).
- Registro de usuarios, roles, multiusuario, OAuth, MFA, correo de recuperación o autorización por origen LAN. La IP de origen no sustituye al login.
- Nuevos proveedores, IPv6 ni nuevos comandos: DonDominio sigue siendo el único adaptador; la UI representa el proveedor de cada registro sin simular soporte adicional.
- Editar credenciales de proveedores, tokens, URL/CA del Agent, `providers` o `hub` desde el navegador; el aprovisionamiento y la rotación siguen por CLI/entorno (ADR-0005/0006).
- Nuevos sondeos de Internet, resolución desde el Hub o consultas DNS a destinos elegidos en el navegador. Sin SSE, WebSockets, React ni nueva infraestructura.
- Rediseñar la landing, modificar su DTO, optimizar PNG o cerrar pruebas manuales pendientes de slices anteriores.

## Precondiciones y supuestos

- Slice4 fusionado: PR [#6](https://github.com/oxxido/dynDNSarr/pull/6), merge `e7a20c54a8074012ad751b09cffdcd8c085ebe99`, verificado con `gh pr view 6` el 2026-10-20. El `main` local coincide con `origin/main` después de `git fetch origin`.
- Se ejecuta la Fase 4 completa en la rama del plan, con commits pequeños y un PR; no se adelanta el slice6. La ejecución fue encargada por el usuario el 2026-10-20.
- Se parte de un solo administrador con acceso a todos los Agents registrados. Es la opción mínima para la instalación personal actual; no se introduce un modelo de roles.
- El navegador accede por HTTPS, también en LAN. TLS termina en el proxy externo; el upstream del Hub sigue restringido. Las pruebas usan HTTPS y certificados locales, sin desactivar `Secure` ni la verificación TLS.
- La referencia visual disponible, [design_docs/index.html](../../design_docs/index.html), solo contiene la landing. Las nuevas pantallas mantienen marca, colores, paneles, tema `Sistema / Claro / Oscuro`, responsive, foco visible y movimiento reducido; no existe una maqueta privada aprobada que haya que reproducir.

## Decisiones previas

Decisiones implementadas: [ADR-0009](../decisions/0009-administracion-web.md) (identidad/control web) y [ADR-0010](../decisions/0010-monitorizacion-privada.md) (diagnóstico/monitorización).

### D1. Administrador y sesiones

- CLI propuesta: `dyndnsarr-hub admin-create <username>` y `admin-password <username>`, con contraseña por entrada oculta y confirmación; nunca argumento, valor por defecto ni salida de logs. Un segundo administrador se rechaza. El cambio de contraseña revoca todas las sesiones. Sin administrador, el Hub sigue sirviendo la landing y el login informa de que falta aprovisionamiento, sin permitir alta web.
- Usar `argon2-cffi` para Argon2id; comprobar instalación en Python 3.11 e imagen del Hub y medir el coste antes de fijar parámetros documentados. Contraseñas de 12–128 caracteres; límite de cuerpo antes de parsear. Usuario desconocido y contraseña incorrecta producen el mismo error y una comprobación de hash equivalente.
- Sesión opaca de 32 bytes aleatorios; guardar solo su hash en SQLite. Cookie `__Host-dyndnsarr_session`, `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, sin `Domain`. Caducidad absoluta 12 h y por inactividad 30 min; comprobar ambas en cada acceso. Logout revoca en servidor y borra la cookie; reiniciar el Hub conserva las sesiones válidas y sus vencimientos.
- El formulario de login usa una sesión anónima de 10 min con CSRF; al autenticar, revocar esa sesión y generar otra identidad y otro CSRF. Sin fijación de sesión ni sesión autenticada en `localStorage`.
- Cinco fallos de login en 15 min bloquean nuevos intentos durante 15 min, con `429` y `Retry-After`; contador persistente único para el administrador, también para usuarios inexistentes, sin filas por nombres arbitrarios. No depende de `X-Forwarded-For`. El CLI de recuperación permite restablecer el bloqueo. Purgar sesiones vencidas y contadores antiguos con límites, sin servicio extra.

### D2. Autorización, CSRF y separación de superficies

- `/admin/login` es el único acceso de entrada sin autenticar; `/admin/static/*` sirve únicamente assets sin datos. El resto de páginas privadas exige sesión antes de consultar recursos; HTML sin sesión redirige al login y `/admin/api/v1/*` devuelve `401`. Los recursos inexistentes dan `404` solo después de autenticar. No aceptar redirecciones de retorno externas.
- Todas las mutaciones web, incluidos login y logout, exigen token CSRF ligado a la sesión, en campo oculto o cabecera; falta/error → `403` antes de efectuar operaciones. Comprobar `Origin` contra un origen HTTPS configurado (`DYNDNSARR_HUB_ADMIN_ORIGIN`), con `Referer` como respaldo; no derivarlo de cabeceras de proxy no confiables. `SameSite` es una protección adicional.
- `Cache-Control: no-store` en login y respuestas privadas, incluidos errores/redirecciones; `Referrer-Policy: same-origin` (Chrome envía `Origin: null` en formularios con `no-referrer`, ver evidencia de ejecución), CSP por nonce, `frame-ancestors 'none'`, `nosniff`, autoescape y ausencia de HTML sin escapar para eventos/errores/configuración. Desactivar la documentación automática pública `/docs`, `/redoc` y `/openapi.json` del Hub para no publicar el contrato administrativo.
- Ningún GET lanza comprobaciones, reenvía comandos ni aplica configuración. Las escrituras de sesión para autenticación/caducidad no son acciones operativas. Un token Agent–Hub no inicia sesión web, y una cookie web no autoriza `/api/v1/agents/*`; esas rutas conservan su autenticación de servicio.
- Con `PUBLIC_ENABLED=false`, `/`, `/public/*` y `/static/*` siguen dando `404`; `/admin` y sus assets funcionan. Montar assets privados en `/admin/static` sin reabrir `/static`. El código público y sus consultas siguen leyendo únicamente `public_*`, sin importar repositorios privados ni contener sesiones, usuarios o auditoría.

### D3. Configuración y diagnóstico: lo que falta en el contrato

`AgentStatus` solo contiene IP y estado resumido por registro; no incluye las respuestas de los resolvers ni el contenido de la configuración. No intentar reconstruirlos con eventos de cambio ni importar el paquete Agent en el Hub.

- Añadir `GET /internal/v1/config` autenticado: versión/hash activos y contenido efectivo de la lista administrable de 3b (`check_interval_seconds`, `ip_sources`, `dns`, `retention`, `update`, `records`). Construirlo por lista blanca desde `ConfigHolder`; excluir siempre `providers`, `hub`, secretos y rutas locales. Permite editar incluso cuando aún manda el TOML (versión 0).
- Añadir `GET /internal/v1/diagnostics` autenticado, con filtro opcional de registro existente y paginación de hasta 100 observaciones por página. Devuelve las últimas muestras almacenadas de fuentes IPv4 y resolvers, timestamps/identificadores estables, direcciones resueltas (la columna `dns_checks.ip` contiene una lista separada por comas), códigos seguros y última actualización aceptada/verificada cuando existe. Los filtros son consultas SQLite parametrizadas; no admiten URLs, comandos ni destinos de red.
- Modelos Pydantic explícitos en `shared/control.py`, con `extra="forbid"`, campos acotados y sin diccionarios de errores arbitrarios. Reutilizar auth/TLS y límites de 3b, no cambiar `AgentStatus`, el JSON canónico ni el vector de hash existente. Leer configuración/diagnóstico no hace red ni modifica el motor.
- Extender la tarea de refresco de slice4 para conservar, por separado, un snapshot privado de `AgentStatus`, configuración efectiva y diagnóstico. Páginas y GET privados leen la caché con su edad; un POST «Actualizar diagnóstico» solicita la lectura acotada al Agent, y `check_dns` ejecuta la comprobación real. Un Agent anterior sin las rutas nuevas mantiene su estado disponible y muestra «diagnóstico/configuración no compatibles»; no rompe el refresco público ni inventa datos.
- Toda red sucede fuera de una transacción; después se guardan snapshots privados y la proyección pública en transacciones cortas. Mantener un solo refrescador y un worker. No copiar la IP completa a `public_*`; fallo del refresco privado no impide actualizar la proyección pública obtenida de `status`.
- Diagnóstico = IP confirmada y su edad, IP por fuente, respuestas/errores por resolver, comparación con la esperada, discrepancias, última aceptación del proveedor, última verificación DNS y estado del enlace/outbox. Identificar resolvers externos configurados; no afirmar que se consultó el resolver del sistema si no está configurado. Un Agent inalcanzable es `agent_unreachable` con última observación, no prueba de caída de Internet.

### D4. Configuración y acciones desde la UI

- Formularios para la lista de registros (nombre, habilitado, `monitored`/`managed`, proveedor), frecuencia, fuentes/resolvers y opciones de verificación/retención del Agent. Mostrar la configuración efectiva, la última deseada y su estado `pendiente/aplicada/rechazada`; el formulario parte de la efectiva, sin confundir una versión rechazada con la activa.
- El Hub valida estructura, tamaños y campos permitidos; el Agent conserva la única validación semántica, de credenciales locales y aplicación atómica. Proveedor seleccionable solo entre adaptadores existentes; un registro `monitored` no lleva proveedor. No se desplaza el modelo DDNS al Hub.
- Antes de enviar una nueva versión, comprobar en transacción la versión deseada que vio el formulario; formulario obsoleto → `409`, sin sobrescribir otra edición. Si cambia la versión activa tras el refresco, exigir recargar. El envío crea versión/hash en el Hub y usa el `PUT` existente. Un timeout conserva estado pendiente y ofrece reenviar **la misma** versión; un rechazo conserva la config activa. No hay reenvío automático al cargar una página.
- `check_dns` y `sync_ddns` actúan por Agent, como en 3b; no prometer selección de un único registro que el ejecutor no soporta. Mostrar el alcance antes de enviar. `sync_ddns` solo se ofrece si hay `managed` habilitados; el Agent vuelve a comprobarlo y sigue excluyendo `monitored` aunque se manipule el formulario.
- Cada formulario de acción lleva un identificador de operación emitido por el Hub, ligado a sesión, agente, tipo y contenido. Persistirlo con la operación antes de hacer red: doble clic/reenvío devuelve el mismo comando o versión, sin nuevo efecto; identificador reutilizado con otro contenido → `409`. No confiar en un `requested_by` del navegador: generarlo como `admin:<id>`.
- Reutilizar `AgentClient`, `control_repo` y los manejadores de resultados. Extraer únicamente la orquestación necesaria de `cli.py` a servicios asíncronos reutilizables, manteniendo la interfaz CLI. Un timeout no es un fallo definitivo; consultar el mismo `command_id` por POST explícito o esperar el evento, sin ejecutar de nuevo. Mostrar aceptación, ejecución y resultado por separado.
- Los ajustes del Hub editables aquí son los umbrales de D5. Publicación de la landing, intervalo de refresco, origen HTTPS, rutas y secretos siguen en entorno; mostrarlos de forma segura y documentar el reinicio cuando corresponda. No introducir una UI genérica de variables de entorno.

### D5. A4: estados configurables y uptime observado

- Persistir una política global de monitorización en el Hub: antigüedad máxima de observación **900 s**, umbral de `mismatch` crítico **3600 s** y factor de refresco obsoleto **3**, conservando los valores de slice4. Validar valores positivos, factor ≥ 1 y antigüedad máxima al menos igual al mayor intervalo de comprobación conocido; advertir si falta la config de algún Agent. Registrar los cambios, sin reescribir el pasado.
- Mantener una única regla pura de estado parametrizada para uso público y privado, a partir de proyecciones sin secretos; estado por registro = regla aplicada a ese registro, agregado con `critical > degraded > unknown > healthy`. La capa pública no importa servicios privados. Conservar los casos y bordes de ADR-0007: datos vencidos/ausentes → `unknown`; error definitivo o mismatch persistente → `critical`; incertidumbre/no verificado → `degraded`; consistente → `healthy`.
- Las muestras se guardan con el instante real de observación y política utilizada. Releer el mismo snapshot no crea otra comprobación satisfactoria ni aumenta contadores: deduplicar por identificadores/timestamps de origen, agente y registro. No convertir cada refresco del Hub en un nuevo check del Agent.
- Métricas privadas en ventanas exactas de 24 h, 7 d y 30 d: tiempo `healthy/degraded/critical/unknown`, cobertura y porcentaje de tiempo saludable **sobre tiempo conocido**. La evidencia conserva su timestamp original, pero la cobertura empieza cuando se recoge en el Hub y cubre hasta la siguiente, como máximo hasta vencer su intervalo de comprobación y antigüedad configurada; el resto es desconocido. Recortar intervalos a la ventana, sin extenderlos por encima de un fallo explícito de observación ni a través de reinicios/huecos sin datos.
- Mostrar numerador, denominador y cobertura: 1 h saludable observada dentro de 24 h puede dar 100 % sobre 1 h, con cobertura 4,17 %, nunca «24 h disponibles». Sin tiempo conocido, porcentaje `null`/«Sin datos». Registros nuevos, deshabilitados y eliminados no aportan tiempo saludable fuera de su periodo observado.
- Separar salud del servicio, resolución DNS observada, estado de las fuentes IPv4 y tiempo desde el último cambio de IP. El proceso muestra su arranque/reinicio, no disponibilidad continua demostrada. No hay un sondeo independiente de Internet: identificar esa métrica como «Sin medición independiente» y no deducir una caída por fallo de fuentes o del enlace Hub–Agent.
- Incidencia observada = intervalo consecutivo `degraded`/`critical` por agente/registro; `healthy` confirma recuperación, `unknown` corta la evidencia con final desconocido. Mostrar duración observada y recuperación solo cuando hay ambas observaciones, sin rellenar el hueco. Derivar intervalos de las muestras, sin nueva máquina de estados redundante.
- Contadores de cambios de IP y renovaciones desde eventos deduplicados; checks satisfactorios/fallidos solo desde muestras distintas realmente recogidas, rotulados «observados por el Hub». Las muestras se conservan 30 días, con purga acotada en el refresco. No reconstruir métricas privadas anteriores al slice ni recuperar observaciones perdidas durante una caída del Hub: esos intervalos quedan desconocidos. Las barras públicas de 7 d y su `coverage` actual mantienen su contrato; el porcentaje privado por duración es una métrica diferente.

### D6. Historial, auditoría y navegación

- Rutas HTML: `/admin/` (resumen y selector de Agent), `/admin/records`, `/admin/history`, `/admin/diagnostics`, `/admin/config` y `/admin/commands/{command_id}`. Login/logout en `/admin/login` y `/admin/logout`. Sin enlaces a integraciones aún inexistentes ni datos de demostración.
- Formularios normales POST con redirección tras éxito; JS solo donde aporte (tema y controles de formulario). Usar los patrones visuales de la referencia sin copiar sus datos simulados o atributos incompatibles con CSP. Tablas legibles en móvil, etiquetas para formularios, errores junto al campo y navegación por teclado. Las fechas se muestran en UTC rotulada, como en slice4.
- Historial paginado por cursor estable (50 filas por defecto, máximo 100), con filtros por agente, rango UTC, registro y tipo de evento. Mostrar `occurred_at` y `received_at`, separar cronología de observación y recepción, y distinguir manual/automático solo cuando existe correlación con un comando; en el resto, origen «no determinado», sin inferencias por cercanía de hora.
- Auditoría propia persistida: instante UTC, actor, acción, recurso, ID de operación/comando/versión, resultado y código seguro. Login correcto/fallido/bloqueado, logout, recuperación CLI, cambios de umbrales/config y comandos quedan registrados; para una operación externa registrar intento antes del envío y resultado después, conservando un resultado incierto ante timeout o reinicio. Nunca guardar contraseña, cookie, CSRF, tokens ni cuerpos de formularios/configuración en auditoría o logs.
- Los eventos entrantes admiten payloads genéricos: la UI muestra detalles mediante campos permitidos por tipo y códigos seguros, sin volcar JSON arbitrario. La IP completa autorizada se muestra solo en pantallas privadas apropiadas; un payload desconocido no abre una vía para publicar secretos. Añadir índices para filtros reales, sin rehacer los repositorios existentes.

## Plan de implementación

1. **Fijar contratos y migraciones.** Añadir pruebas de contrato de config/diagnóstico, límites, autorización y ausencia de secretos. Migraciones aditivas del Hub a partir de `0005`: administrador/sesiones/bloqueo, auditoría/idempotencia, caché privada/política/muestras e índices. Dividir SQL por cambios coherentes. Verificar actualización desde BD 0004 con eventos, comandos, configuración y `public_*` intactos.
2. **Identidad y autenticación.** CLI, hash, sesiones, CSRF previo al login, throttling y recuperación. Verificar expiración con reloj controlado, rotación, logout, reinicio y revocación; ninguna ruta privada se expone antes de tener estas protecciones.
3. **Consultas del Agent.** Implementar los dos GET de D3 y métodos del cliente; pruebas con Agent real en proceso, token/CA incorrectos, paginación, filtro inválido y llamadas sin acceso a red/proveedor. Mantener compatibles los contratos de 3b.
4. **Caché y monitorización.** Ampliar el refrescador sin duplicarlo, guardar proyecciones separadas y deduplicar observaciones. Parametrizar la regla común, política persistida, cálculo de ventanas/cobertura/incidencias y purga a 30 días. Verificar huecos, datos obsoletos, versión antigua del Agent, error privado con estado público disponible y reinicios.
5. **Consultas privadas y UI de lectura.** Dashboard, registros, historial, diagnóstico e incidencias usando la caché; misma autorización en HTML/JSON y enlaces directos. Verificar valores reales, filtros/paginación, escape, respuestas sin caché y funcionamiento con página pública apagada.
6. **Configuración y acciones.** Servicios compartidos con CLI, formularios y endpoints mutantes con CSRF, control de edición obsoleta, ID de operación y auditoría. Verificar rechazo atómico, reenviar misma versión, doble clic, timeout y resultado por evento, sin cambios en las garantías DDNS del Agent.
7. **Empaquetado y despliegue.** Incluir paquetes, plantillas privadas y assets necesarios en el wheel; añadir dependencia de hash/formularios solo según uso y ejemplos de `ADMIN_ORIGIN`. Actualizar Compose sin secretos, scripts de verificación y operaciones. No modificar `.env` real ni configurar el servidor/proxy durante este encargo.
8. **Validación y entrega.** Suite completa, lint/formato, wheels y CI Docker; revisión visual escritorio/móvil y temas, e2e de login → diagnóstico → configuración → acción → logout y regresión pública. Registrar evidencia real, commits pequeños, push y PR contra `main`; no fusionar sin autorización.

## Criterios de aceptación

1. Sin sesión válida no se obtiene ningún dato privado por HTML, JSON, detalles de comandos/config ni rutas alternativas. Un token de servicio no sirve como sesión y una cookie no sustituye el token de ingestión. Sin admin no hay alta web ni credenciales por defecto.
2. Solo el hash de la contraseña y de las sesiones queda en SQLite. Cookie con todos los atributos de D1; login cambia su identidad; expiración, logout y cambio de contraseña invalidan el acceso también después de reiniciar. Cinco fallos activan el bloqueo persistente y la recuperación CLI funciona sin mostrar secretos.
3. POST sin CSRF válido o con origen ajeno falla sin operación, versión, comando ni llamada al Agent. Ningún GET operativo produce esos efectos. Cuerpos sobredimensionados y recursos no autorizados se rechazan antes de actuar.
4. Dashboard y registros muestran estado, modo, proveedor, IPv4 completa, edad y errores reales de dos Agents con varios registros; al caer uno aparece `agent_unreachable` y sus datos envejecen a `unknown`. No se presenta como caída demostrada de Internet.
5. El diagnóstico muestra respuestas discrepantes y fallidas por resolver con sus timestamps, IP esperada, aceptación pendiente y verificación posterior. Leerlo no hace DNS ni llama al proveedor; el POST de `check_dns` sí actualiza observaciones mediante el comando existente. Las consultas se paginan sin pérdidas/duplicados en un snapshot estable.
6. Editar desde la versión 0 (TOML), enviar configuración válida y recibir ack muestra la versión aplicada. Config inválida, sección prohibida, credenciales faltantes y edición obsoleta conservan la activa; timeout queda pendiente y el reenvío conserva versión/hash. Cambio de modo a `managed` debe ser explícito.
7. Un doble POST de comando/configuración crea una sola operación; respuesta perdida y reenvío no ejecutan dos veces. Un `sync_ddns` con mezcla `managed`/`monitored`, incluso manipulando el formulario, nunca escribe los `monitored`; sin `managed` habilitados se rechaza sin llamada al proveedor.
8. Cambiar los umbrales modifica las reglas públicas y privadas para nuevas evaluaciones con valores validados; tras reiniciar se conservan. Los defaults reproducen los casos de slice4 y los cambios no recolorean las muestras históricas.
9. Ventanas 24 h/7 d/30 d: pruebas con reloj fijado verifican duración, recortes, cobertura, porcentajes nulos sin datos, huecos y recuperación. Releer un snapshot no aumenta checks; 1 h saludable en 24 h muestra 4,17 % de cobertura. No se rellena historia previa ni se confunde la cobertura por barras pública con la privada por duración.
10. El historial filtra por fechas, agente, registro y tipo, conserva orden estable y eventos duplicados se ven una vez; distingue observado/recibido. Login y mutaciones tienen auditoría persistente con actor e ID, sin contraseñas, sesiones, CSRF, tokens o cuerpos de config.
11. Canarios privados (IP, token, usuario, payload malicioso) no aparecen en HTML/DOM/JSON/cabeceras/logs públicos ni en `public_*`; los textos se escapan en privado y no se vuelca payload arbitrario. CSP y `no-store` cubren también errores. Con `PUBLIC_ENABLED=false`, el admin funciona y lo público sigue apagado.
12. Migrar una BD 0004 con datos mantiene eventos/config/comandos/proyección pública; reinstalación y reinicio conservan datos y vencimientos. Wheel limpio incluye todas las pantallas; CI 3.11 y construcción/arranque de Docker pasan, con `/healthz` compatible y un solo worker.
13. Revisión manual del usuario tras proxy HTTPS (operaciones): aprovisionamiento, login/logout, cookie, móvil/escritorio y tres temas, estado real, cambio inválido y comprobación DNS. Las mutaciones reales contra DonDominio requieren un registro `managed` de prueba del usuario; no se ejecutan para validar el plan.

## Pruebas

- `tests/unit`: reglas parametrizadas y bordes, expiración/bloqueo/CSRF, métricas por duración, deduplicación de observaciones, intervalos de incidencia, saneado de detalles y repositorios/migraciones.
- `tests/contract`: nuevos DTO privados y límites; contratos existentes `AgentStatus`, hash canónico, eventos, `/healthz` y `PublicStatusDTO` sin cambios.
- `tests/integration`: Hub/Agent reales con `ASGITransport`, sesiones/cookies bajo origen HTTPS, matriz de autorización de todas las rutas, CSRF/origen, CLI de recuperación, formularios inválidos, idempotencia y edición concurrente. Cliente TLS real con `TlsAgent`/CA incorrecta. Proveedor falso que registra llamadas para demostrar que `monitored` nunca se escribe.
- `tests/e2e`: Hub y Agent como procesos, proxy TLS local de prueba dentro del proyecto para el navegador, login y recorrido completo, reinicio, respuesta perdida, outbox y fallo del Hub sin interrumpir el Agent. No usar servicios ni credenciales reales. DOM/capturas con navegador si está disponible; registrar la revisión visual pendiente si no lo está.
- Comandos previstos al ejecutar: `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/pytest -m "not e2e"`, `.venv/bin/pytest`, `scripts/verify-wheels.sh` y `scripts/verify-compose.sh` (Docker/CI). Las pruebas de comportamiento se escriben antes de exponer las operaciones correspondientes.

## Documentación a actualizar

- `docs/operations.md`: aprovisionamiento/recuperación, sesiones/bloqueo, origen HTTPS y CSRF, rutas privadas frente a públicas y de servicio, upgrade Agent → Hub, uso de pantallas, versiones/comandos pendientes, métricas y limitaciones de cobertura, backup/restore y comprobación manual.
- `docs/conventions.md`: autenticación web, auditoría, servicios compartidos, contratos privados y regla común de estado. `docs/decisions/`: ADR de autenticación y monitorización; actualizar referencias de ADR-0007 sobre A4 y mantener el DTO público.
- `.env.example`, `apps/hub/compose.yaml`, dependencias/package-data y scripts de verificación según los cambios efectivos; nunca `.env` ni credenciales reales.
- `ESTADO.md`, `docs/slices/README.md`, este slice y `plan.md` (A4 de umbrales/uptime resuelta solo cuando se implemente y verifique). Actualizar arquitectura únicamente donde una decisión implementada lo requiera, sin detallar slice6.

## Riesgos y migraciones

- **Tamaño del slice:** incluye una superficie administrativa y nuevos datos observados. Mantener el orden de pruebas/seguridad antes de acciones y los límites de alcance; no añadir integraciones ni framework genérico. Si aparece una dependencia que impida completar la Fase 4, registrar el bloqueo antes de alterar el alcance.
- **Exposición por proxy:** las rutas `/admin/*` necesitan HTTPS y controles del Hub aun en LAN. La receta de proxy público de slice4 sigue sin reenviar administración ni ingestión; la publicación privada se configura por separado. No confiar en cabeceras reenviadas de cualquier origen.
- **Bloqueo de login:** el contador global simplifica el MVP y permite denegación temporal por intentos ajenos. Documentar acceso privado y recuperación CLI; no introducir identificación por IP aparente como sustituto.
- **Datos privados en disco:** snapshots contienen IP completa y configuración administrable; `hub.db` ya contiene tokens API en claro (ADR-0006). Mantener permisos y backups protegidos; nunca reutilizar estas tablas para renderizar público.
- **Compatibilidad de despliegue:** actualizar primero el Agent para los nuevos GET; un Agent antiguo conserva `status`, pero no habilita edición inicial ni diagnóstico detallado. Mensaje explícito sin borrar la última caché válida ni romper DDNS autónomo.
- **Observación incompleta:** Hub apagado o refrescos fallidos pueden perder muestras entre lecturas; la retención del Agent puede ser menor que la ventana. Cobertura y contadores se limitan a lo recogido por el Hub. No prometer historial exhaustivo de cada check ni 100 % de Internet.
- **Coste del refresco:** las nuevas consultas son paginadas y de datos locales; limitar páginas por pasada y continuar con cursor en la siguiente, sin bucles de recuperación ilimitados ni tareas nuevas por navegador. Registrar atraso y mostrar datos obsoletos como desconocidos. Sin transacciones abiertas durante red.
- **Migraciones aditivas del Hub:** tablas privadas nuevas, sin migración destructiva de datos existentes; no se prevé migración del Agent porque las consultas usan sus tablas actuales. Test de backup/restauración con API SQLite y rollback mediante backup consistente, no copiando un `.db` con WAL activo.
- **Historia y política:** las muestras privadas comienzan al actualizar; las públicas de slice4 no tienen el detalle por registro ni versión de política y no se convierten en observaciones privadas. Ajustar umbrales afecta a nuevas evaluaciones, no corrige retrospectivamente incidentes históricos.

## Evidencia

**Planificación, 2026-10-20:**

- Leídos `AGENTS.md`, roadmap, estado, convenciones, índice/plantilla y slice4; arquitectura solo en secciones afectadas, capítulos de requisitos indicados y ADR-0007. Referencia visual: `design_docs/index.html`; no se leyó `docs/deprecated/` ni `.env`.
- Revisados `shared/control.py`, API/config/tablas de observación del Agent y CLI/cliente/refrescador/tablas/proyección del Hub: diagnóstico y lectura de configuración efectiva todavía no existen; los eventos solo reflejan cambios, no cada comprobación.
- `git fetch origin` y `gh pr view 6 --json number,state,mergedAt,mergeCommit,url,headRefName,baseRefName`: PR #6 `MERGED` el 2026-10-20 a las 08:50:41 UTC; `main` y `origin/main` en `e7a20c5`. Árbol limpio al empezar; rama del slice creada desde esa base.
- Validación documental: 25 enlaces locales existentes en los cuatro documentos afectados, secciones de la plantilla completas, estado del slice coherente con el índice y `git diff --check` sin errores.
- Solo documentación: sin implementación, instalaciones, cambios de entorno ni pruebas de aplicación. La validación de código, CI y pruebas manuales de este archivo quedan pendientes del encargo de ejecución.


**Ejecución, 2026-10-20:**

- Contratos aditivos y lecturas autenticadas en el Agent; configuración efectiva por lista blanca y diagnóstico paginado. Sin modificaciones al ciclo/proveedor ni al contrato existente de status/hash/eventos.
- Migraciones Hub 0005 (administrador, sesiones, bloqueo, auditoría e idempotencia) y 0006 (política pública numérica, caché y muestras privadas). Upgrade desde 0004 con datos y backup/restauración cubiertos por tests.
- Panel privado Jinja2 y API de respaldo, sesiones/CSRF/origen, recuperación CLI, servicios compartidos de control, política persistida, historial filtrado y métricas/incidencias con cobertura. La recogida empieza a cubrir tiempo al llegar al Hub; no rellena retrospectivamente caídas. Observaciones identificadas también por timestamp para admitir IDs reutilizados tras purga del Agent.
- Verificación inicial: 773 pruebas rápidas existentes pasaban antes de implementar. Primera suite rápida ampliada: 802 passed. E2e nuevo de procesos con proxy HTTPS/CA local: login → configuración → comando → Hub apagado (Agent continúa) → reinicio con sesión persistente → logout: passed. Ambos enlaces TLS verificados, sin servicios ni credenciales reales.
- Verificación dirigida tras ajustes finales de seguridad/UI: 38 passed (sesiones/CSRF, acciones con Agent real, monitorización, paginación y compatibilidad de Agent antiguo). Los POST duplicados conservan ID y una respuesta perdida se reconcilia sin reejecutar; `sync_ddns` solo llama al proveedor para `m.example.com` (managed), nunca para monitored.
- `scripts/verify-wheels.sh` ampliado y ejecutado desde un entorno limpio: Agent/Hub arrancan, todos los paquetes/PNG/plantillas viajan, login real de prueba y cinco vistas privadas responden. `ruff check`, `ruff format --check`, `bash -n` de scripts y `git diff --check` correctos.
- Chrome instalado, automatizado con Playwright de QA (solo venv local, sin dependencia nueva de runtime/CI): 20 capturas de las cinco vistas en escritorio/móvil y claro/oscuro, más login; sin desbordamiento horizontal. Navegación contra las apps mediante transporte ASGI interceptado, sin desactivar TLS. Login/logout, campos iniciales de verificación, filas dinámicas y tema Sistema con cambios de preferencia/persistencia comprobados. El e2e separado cubre el proxy HTTPS real con verificación de certificados.
- Hallazgos corregidos durante QA: Chrome envía `Origin: null` en formularios con política no-referrer (privado cambiado a same-origin manteniendo CSRF y rechazo de null); colisión de `c.update` con el método del diccionario en Jinja2 (acceso por clave para precargar los campos de verificación). Añadidas regresiones automatizadas.
- Argon2id medido localmente: hash 85,4 ms / verificación 71,2 ms (64 MiB, t=3, p=4; no extrapolar al servidor de despliegue).
- Suite completa local: **836 passed**, sin skips, en 269,81 s (Python 3.12); los últimos ajustes de cabeceras/errores y precarga del formulario están además cubiertos por las 38 pruebas dirigidas posteriores. Una advertencia de deprecación preexistente de Starlette/TestClient, sin fallos. Resultados de CI (Python 3.11/3.12 e imagen Docker) en [los checks del PR #7](https://github.com/oxxido/dynDNSarr/pull/7/checks). La comprobación manual §9.5 en la instalación del usuario y cualquier mutación contra DonDominio real siguen a cargo del usuario; no se ejecutaron aquí. La imagen Docker se verifica en CI, sin modificar el servidor Docker local.

- Entrega inicial: rama subida a `origin/slice/05-hub-privado-diagnosticos`, PR [#7](https://github.com/oxxido/dynDNSarr/pull/7) abierto contra `main`, sin merge. Verificación documental: 73 enlaces locales existentes y diff sin errores. La revisión de CI y del PR se consulta en sus checks; la prueba manual de la instalación sigue pendiente según §9.5.
- Revisión final del historial: selector «Todos los Agents» coherente con la consulta sin filtro y conservación de fechas/tipo consultados. Regresión de recorrido privado/historial: 11 passed; reglas públicas con umbrales personalizados de antigüedad/mismatch/refresco: 31 passed. CI del commit final en los checks del PR #7.

- **Merge de la implementación principal verificado:** PR #7 fusionado por `oxxido` el 2026-10-20 a las 09:43:31 UTC, commit `aa66d0391c316ed6e6d82e2910ba8af7b49ba55a`. CI del head fusionado `127fdbe`: **837 passed en Python 3.11 y 3.12**, wheels y `hub-image` correctos ([ejecución](https://github.com/oxxido/dynDNSarr/actions/runs/39402248036)).
- **Ajuste final posterior al merge:** `fix/05-filtros-historial`, creada desde `main` actualizado (`aa66d03`). Contiene exclusivamente coherencia/persistencia de filtros del historial, su regresión y prueba de umbrales personalizados; no cambia migraciones ni DDNS. Pruebas locales: 11 de integración + 31 de reglas; Chrome repetido tras el ajuste, correcto. Entregado en el PR complementario [#8](https://github.com/oxxido/dynDNSarr/pull/8), contra `main`; evidencia de CI en [sus checks](https://github.com/oxxido/dynDNSarr/pull/8/checks). El agente no fusiona PRs sin autorización.
