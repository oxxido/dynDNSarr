# ADR-0010 — Diagnósticos privados, política común y uptime por duración

**Fecha:** 2026-10-20

## Contexto

ADR-0007 proyectaba datos públicos sin IP completa, con umbrales provisionales y cobertura por barras. El `AgentStatus` de ADR-0006 no contiene respuestas por resolver ni configuración efectiva; los eventos solo reflejan cambios y no permiten contar todas las comprobaciones.

## Decisión

1. Extender aditivamente `/internal/v1` con GET autenticados de configuración efectiva administrable y diagnóstico paginado. DTO tipados, sin `providers`, `hub`, credenciales ni rutas locales. No modificar el contrato `AgentStatus`, el hash canónico ni el motor DDNS. El diagnóstico lee tablas existentes: no ejecuta DNS ni proveedor.
2. Un único refrescador consulta estado/config/una página de hasta 100 muestras por Agent y pasada. Caché privada separada de `public_*`; las pantallas GET no hacen red. Un POST explícito puede refrescar. Agentes anteriores conservan `status` y muestran consultas nuevas no compatibles. Los fallos privados no revierten la proyección pública.
3. Política común persistida en `public_monitoring_policy` (solo números, sin secretos), legible por público sin importar repositorios privados: antigüedad 900 s, mismatch crítico 3600 s y factor de refresco 3. Se conservan las reglas/precedencia de ADR-0007 y se valida la antigüedad contra intervalos conocidos. Cambios auditados, sin reescribir muestras históricas.
4. Muestras privadas con timestamp de evidencia, instante de recogida, estado, límite de cobertura y versión de política. Deduplicación de estado por Agent/recurso/evidencia; observaciones por Agent/tipo/ID/timestamp (los IDs de observación del Agent pueden reutilizarse tras purgar). La cobertura empieza al recoger la evidencia y acaba como máximo en su vencimiento según intervalo del Agent y antigüedad configurada. Nunca se rellena retrospectivamente un hueco del Hub.
5. Ventanas 24 h / 7 d / 30 d: porcentaje saludable sobre tiempo conocido, cobertura, duraciones por estado y denominadores explícitos. Sin tiempo conocido, porcentaje nulo. Fallos, reinicios y retirada de registros cortan evidencia; repetir una caché no prolonga disponibilidad. Incidencias derivadas de tramos degradados/críticos; recuperación solo con observación saludable contigua.
6. Separar salud del servicio y respuestas de fuentes IPv4/resolvers. Una respuesta no demuestra disponibilidad independiente de Internet ni coincidencia con la IP esperada. Páginas incompletas no prueban éxito. Contadores limitados a muestras distintas recogidas y eventos deduplicados, sin inventar historia previa. Retención privada de 30 días con purga acotada; retención general del Hub en slice6.

## Consecuencias

A4 de umbrales/uptime queda resuelta. La cobertura privada por duración y la pública por barras son diferentes y están rotuladas. Un Hub recién actualizado arranca sin historia privada. Actualizar primero Agent y luego Hub; migraciones aditivas 0005/0006 del Hub, sin migración del Agent. Se conserva la garantía de que los datos y consultas públicos no contienen IP completa, identidad web o auditoría.

La lectura de configuración permite editar el TOML efectivo versión 0; el Agent mantiene la última configuración válida ante rechazo. El proveedor sigue siendo DonDominio y `monitored` nunca escribe. Ver [operaciones §9](../operations.md#9-hub-privado-slice5) y [slice5](../slices/05-hub-privado-diagnosticos.md).
