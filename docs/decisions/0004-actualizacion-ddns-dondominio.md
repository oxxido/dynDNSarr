# ADR-0004 — Actualización DDNS `managed` con DonDominio

**Fecha:** 2026-10-17

## Contexto

El slice 2b ([slices/02b-actualizacion-ddns-managed.md](../slices/02b-actualizacion-ddns-managed.md)) hace que el Agent escriba en DNS. Resuelve A1 de [architecture.md](../architecture.md) §14 y fija reglas que los slices siguientes (Hub, alertas) heredan. El API de DonDominio se verificó con la [documentación oficial](https://dondominio.dev/en/dondns/docs/api/) y con llamadas reales el 2026-10-15 (ver «Hallazgos del sondeo real» del slice).

## Decisión

1. **API (D1, A1).** `GET https://dondns.dondominio.com/json/` con `user`, `host`, `ip`, `lang=en` y la clave en `password`. Se envía siempre `ip` explícita (la IP ya confirmada por consenso, ADR-0003); sin ella DonDominio usaría la de quien llama. El éxito es HTTP 200 con `success: true` estricto y `host.value` igual a la IP enviada. Los errores llegan con HTTP 200 y se clasifican por el texto exacto del mensaje (`lang=en` fijo): `Wrong key`/`Incorrect data` → `auth_failed` y `The DNS Zone doesn't exist`/`Domain not found` → `host_not_found`, ambos definitivos; cualquier otro texto → `rejected`, reintentable. Un 429 llega con HTML, no JSON. El API no valida `ip`, de modo que solo se le envía una IPv4 global confirmada.
2. **Credenciales y logs (D4).** `DYNDNSARR_AGENT_DONDOMINIO_USER` y `_KEY` como `SecretStr`, solo por entorno (`EnvironmentFile` 0600), nunca en el TOML. Un registro `managed` habilitado sin ellas impide arrancar. La clave viaja en la URL (el API es GET): `httpx`/`httpcore` en `WARNING`, el adaptador devuelve códigos seguros y nunca el texto de una excepción ni la URL, y un test con valor canario lo vigila.
3. **Backoff (D6).** 60 s inicial, ×2, tope 3600 s, fijos en código; persistido (`next_attempt_at`) para que sobreviva a un reinicio. Configurables solo `verify_retry_seconds` (60) y `verify_timeout_seconds` (900, no medido). Una racha de fallos emite un evento al empezar o al cambiar el código, no uno por reintento. Un error definitivo bloquea los reintentos automáticos para esa IP; `sync-ddns` o un cambio de IP lo levantan.
4. **Qué significa «correcto».** Que el proveedor acepte la llamada no es que el DNS resuelva bien: el registro queda `pending_verification` hasta que los resolvers externos coinciden con la IP (`verified`), o vence el plazo (`dns_verification_failed`). Como máximo una llamada por registro y ciclo, y con la IP ya aceptada no se vuelve a llamar mientras se propaga.

## Consecuencias

- DonDominio no documenta códigos de error: si cambia un mensaje pasa a `rejected` (reintentable, como mucho cada hora) en lugar de bloquear.
- La clave en la URL es el único canal que ofrece el API; la protección son los controles de logs y el canario, no el transporte.
- Un registro `managed` debe ser un A propio en la zona; un CNAME termina en `host_not_found` definitivo y no se detecta antes.
- Sin dependencias nuevas.
