# ADR-0006 — Control Hub → Agent: TLS propio, token, configuración versionada y comandos

**Fecha:** 2026-10-19

## Contexto

El slice 3b ([slices/03b-control-hub-agent.md](../slices/03b-control-hub-agent.md)) permite que el Hub consulte y administre al Agent. Amplía A2 de [architecture.md](../architecture.md) §14 al sentido Hub → Agent: aquí el Agent es el servidor y vive en la Raspberry, sin proxy delante.

## Decisión

1. **TLS del Agent (D1).** Uvicorn sirve HTTPS con `DYNDNSARR_AGENT_TLS_CERT_FILE`/`_KEY_FILE` (certificado firmado por una CA local; receta `openssl` en operaciones). El Hub verifica con `DYNDNSARR_HUB_AGENT_CA_FILE`, solo esa CA. Con token de API y escucha fuera de loopback el Agent no arranca sin TLS; `http://` solo a loopback. Elegido por el usuario (2026-10-18) frente a un Caddy en la Raspberry.
2. **Autenticación (D2).** Token Bearer propio por agente (`ddh_` + 32 bytes), distinto del Agent → Hub. El Agent lo lee de `DYNDNSARR_AGENT_API_TOKEN` y lo compara en tiempo constante; todo fallo es 401 con el mismo cuerpo. El Hub, que debe presentarlo, lo guarda en claro en `agents.api_token` de `hub.db` (0600, volumen `/data`): un `.env` de Dockge es visible en su UI y en `docker inspect`, es global y no por agente, y rotarlo exigiría recrear el contenedor.
3. **Endpoints y acks (D3).** `GET health`, `GET status`, `PUT config`, `POST commands` y `GET commands/{id}` bajo `/internal/v1`. El resultado de `PUT config` va en su respuesta y, además, el Agent emite `config_applied`/`config_rejected`/`config_reset`/`command_completed` por el outbox de 3a: si la respuesta se pierde, el Hub se entera igualmente. No hay `config-ack` ni `command-results`.
4. **Qué administra el Hub (D4).** Solo `check_interval_seconds`, `ip_sources`, `dns`, `retention`, `update` y `records`. `providers` (la URL del proveedor lleva la clave de DonDominio en la petición), `hub`, secretos, TLS y tokens son locales y se rechazan (`forbidden_section`). El Hub trata el contenido como opaco; el único validador es el Agent.
5. **Versionado (D5).** El Hub asigna versiones crecientes y un `sha256` del JSON canónico. El Agent valida todo, inserta la versión en una transacción y cambia la referencia en memoria; lo recoge el siguiente ciclo. Las secciones de la versión activa sustituyen a las del TOML. `config-reset` borra las versiones y deja el TOML.
6. **Comandos (D6).** `check_dns` y `sync_ddns`, con `command_id` persistido, `expires_at` obligatorio y un ejecutor serie. Un comando `received`/`running` al arrancar pasa a `failed/interrupted` y no se reejecuta solo (principio de WoL, §5).

## Consecuencias

- El Hub guarda el token de API en claro: proteger `/data` y sus copias; rotar con `agent-api-token`. Su fuga da acceso solo a la API limitada de ese Agent en la LAN, nunca a DonDominio.
- Un Hub comprometido puede reconfigurar el monitoreo y añadir registros `managed` (se actualizarían con la IP del Agent), pero no cambiar el proveedor ni leer credenciales.
- `expires_at` compara el reloj del Hub con el del Agent: se requiere NTP.
- El certificado del Agent caduca sin aviso; solo corta el enlace, no el DDNS.
- Con TLS activo `/healthz` del Agent también es HTTPS.
