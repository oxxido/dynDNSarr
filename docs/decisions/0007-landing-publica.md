# ADR-0007 — Landing pública: proyección sin IP, estado derivado al leer y publicación

**Fecha:** 2026-10-19

## Contexto

El slice 4 ([slices/04-landing-publica.md](../slices/04-landing-publica.md)) sirve la página pública de estado con datos reales. Resuelve A5 y A7 de [architecture.md](../architecture.md) §14 y fija una versión provisional de A4. `events` del Hub guarda la IP completa (3a): la vista pública no puede leerla.

## Decisión

1. **Fuente del estado (D1).** Una tarea asyncio del Hub consulta cada `DYNDNSARR_HUB_STATUS_REFRESH_SECONDS` (60 s, mínimo 15) el `GET /internal/v1/status` de cada agente con API configurada, con el `AgentClient` de 3b. La petición web nunca llama al Agent: lee lo ya proyectado. Un agente sin API configurada es `Desconocido`.
2. **Proyección sin IP completa (D2).** Tablas `public_renewals`, `public_agent_status` y `public_status_samples`, alimentadas desde la ingestión (renovaciones, solo eventos nuevos) y desde el refresco (estado). Solo se guarda `ip_suffix` (último octeto, 1–2 dígitos); `public_ip` se descarta al leer el estado. No se guarda un hash de la IP ni del sufijo (2³² candidatos lo harían reversible). La capa `public/` no importa `events_repo`.
3. **Estado derivado al leer (D3).** `derive_state` es una función pura con constantes en `public/state.py`: `unknown` sin proyección, proyección vieja (> 3 × intervalo) o comprobación vieja (> 15 min); `critical` con `mismatch` de más de 1 h o error definitivo; `degraded` con IP `uncertain` o un registro sin verificar; `healthy` en otro caso. Con varios agentes, el peor (`critical > degraded > unknown > healthy`). Un Agent inalcanzable es `unknown`, nunca `critical`: no implica que Internet haya caído (§8). El slice 5 los hará configurables.
4. **Nombres y renovaciones (D4, A5).** Una renovación es un `dns_update_succeeded`. Por defecto no se muestra el nombre del registro (resuelve públicamente a la IP completa); `DYNDNSARR_HUB_PUBLIC_SHOW_DOMAINS=true` lo añade, por decisión explícita del administrador.
5. **Historial (D5).** Cada refresco inserta una muestra con el estado derivado (un fallo es `unknown`); 84 barras de 2 h sobre 7 días, peor estado conocido por barra y gris sin datos; `coverage` informa de lo observado. Retención de 30 días.
6. **Publicación (D6).** `GET /` y `GET /public/v1/status`; con `PUBLIC_ENABLED=false` ambas y `/static` dan el mismo 404 que una ruta inexistente. CSS/JS inline con CSP por petición con nonce (sin `'unsafe-inline'`), Jinja2 con autoescape, solo `GET`.
7. **Mascota (A7).** Se usan las bases sin ojos ni boca; una cara visible exige otros assets y otro slice.

## Consecuencias

- La página muestra lo del último refresco (≤ 60 s) más el intervalo de comprobación del Agent; no refresca sola.
- Los umbrales (15 min, 1 h, 3 × intervalo) no están verificados con datos reales: un `critical` falso o tardío es posible. Con un `check_interval_seconds` del Agent mayor que 15 min la página quedaría `Desconocido`; el slice 5 los parametrizará.
- Las renovaciones previas a la migración 0004 no se proyectan: la lista arranca vacía.
- El proxy público debe reenviar solo `/`, `/static/*` y `/public/*`: el resto del Hub (`/api/v1`) exige token pero no debe ser alcanzable desde Internet. El límite de tasa va en el proxy.
