# ADR-0005 — Enlace Agent → Hub: TLS, registro, tokens, contrato y outbox

**Fecha:** 2026-10-19

## Contexto

El slice 3a ([slices/03a-enlace-seguro-eventos.md](../slices/03a-enlace-seguro-eventos.md)) conecta por primera vez el Agent con el Hub: entrega los eventos que el Agent ya guarda. Resuelve A2 y A6 de [architecture.md](../architecture.md) §14 para la dirección Agent → Hub. El slice 3b extiende A2 al sentido inverso (Hub → Agent).

## Decisión

1. **TLS (D1, A2).** Un proxy TLS delante del Hub (decisión del usuario, 2026-10-17); el Hub sigue en HTTP dentro de Docker y se publica solo en loopback o en la red del proxy. El Agent verifica siempre el certificado: con `ca_file` confía únicamente en esa CA; sin él, en el almacén del sistema. `http://` solo a `localhost`/`127.0.0.1` (pruebas). Prohibido `verify=False`. El cliente `httpx` del Hub es propio, separado del de proveedores y fuentes.
2. **Registro (D2, A6).** Manual: `dyndnsarr-hub agent-add <agent_id>` crea el agente y su primer token; el administrador copia URL, `agent_id` y token al Agent. `agent_id` con `^[a-z0-9][a-z0-9-]{0,62}$`. Sin descubrimiento ni código de un solo uso.
3. **Token (D3).** `dda_` + `secrets.token_urlsafe(32)`; el Hub guarda `sha256` en hex (256 bits de entropía: un hash lento no aporta). Un agente puede tener varios tokens vigentes: rotar añade uno sin revocar los anteriores y `agent-revoke` deja solo el último. Todo fallo de autenticación responde 401 con el mismo cuerpo.
4. **Contrato (D4).** `AgentEvent`, `EventBatch`, `EventBatchResult` y `ErrorResponse` en `packages/shared`. El Hub valida cada evento por separado y no valida `type` contra una lista; un evento inválido se rechaza sin bloquear el lote. `acked` incluye los duplicados, de modo que el reenvío es idempotente.
5. **Outbox (D5).** Se reutiliza la tabla `events` del Agent (ya tiene `event_id` único y `sequence` monotónica y se escribe en la transacción del ciclo) con `delivered_at` y `rejected_code`; pendiente = ambos nulos. El Hub deduplica por `(agent_id, event_id)` y responde ACK después del commit.

## Consecuencias

- El token viaja en una cabecera `Authorization`: depende de que el puerto del Hub no se publique sin el proxy TLS (se documenta; el Agent se niega a usar `http://` fuera de loopback).
- El Hub guarda la IP completa en `events` (vista privada). El DTO público del slice 4 no debe leer esa tabla.
- Sin limitación de tasa en el Hub: la aporta el proxy. Un token filtrado se corta con `agent-revoke`.
- Un Agent más nuevo que el Hub no lo rompe (tipos desconocidos se almacenan); campos nuevos obligatorios exigirán `/api/v2`.
- `trustme` entra en el extra `dev` (certificados de prueba). Sin dependencias nuevas en tiempo de ejecución.
