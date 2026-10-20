# ADR-0009 — Administrador local, sesiones y control web

**Fecha:** 2026-10-20

## Contexto

La Fase 4 abre datos y acciones del Hub al navegador. La autenticación de servicio de ADR-0005/0006 no representa a una persona ni protege formularios con cookies. La instalación actual necesita un administrador, sin registro público o infraestructura de identidad adicional.

## Decisión

- Un administrador, creado/recuperado por CLI con entrada oculta. Hash Argon2id mediante `argon2-cffi`, parámetros explícitos 64 MiB / 3 iteraciones / 4 hilos, y rehash si cambian. Parámetros y API contrastados con la [documentación oficial](https://argon2-cffi.readthedocs.io/en/stable/api.html). No contraseñas por entorno ni valores de fábrica.
- Sesión aleatoria de 32 bytes; solo hash SHA-256 en SQLite. Cookie `__Host-dyndnsarr_session` Secure/HttpOnly/SameSite=Lax/Path=/, sin Domain; 12 h absolutas, 30 min de inactividad. Sesión anónima de 10 min para CSRF previo al login; autenticarse rota sesión y CSRF. Logout/recuperación revocan en servidor.
- Cinco fallos por ventana de 15 min bloquean 15 min. Contador global persistente, mismo error/coste de hash para usuario desconocido y contraseña incorrecta; no se identifica al usuario por IP reenviada.
- Toda ruta privada se protege antes de leer recursos/cuerpo. POST exige CSRF ligado a sesión y Origin HTTPS configurado explícitamente (Referer solo si falta Origin). No aceptar Origin null. GET no lanza comandos ni configuraciones. Cookies y tokens de servicio no son intercambiables.
- `no-store`, CSP con nonce y `Referrer-Policy: same-origin` para la superficie privada. La prueba con Chrome detectó que `no-referrer` hace que sus formularios envíen `Origin: null`: se conserva el rechazo estricto y se cambia la política de referencia, que sigue sin enviar referencias a otros orígenes. La política pública permanece `no-referrer`. OpenAPI/docs del Hub no se publican.
- Formularios con ID opaco emitido por el servidor, ligado a sesión, Agent y acción; el primer envío fija hash del contenido. Persistir operación/comando/versión y auditoría antes de la red; duplicados devuelven la misma referencia, cambios de contenido dan 409. Los comandos mantienen la idempotencia del Agent y sus resultados por outbox.
- Configuración versionada con comparación de la versión deseada/activa vista por el formulario. El Agent conserva su validación semántica y las credenciales locales. Consola y web comparten servicios de envío y reconciliación.

## Consecuencias

HTTPS y `ADMIN_ORIGIN` son requisitos del login, también en LAN; sin origen la landing sigue funcionando. Reiniciar mantiene vencimientos y bloqueos. El contador global permite denegación temporal por intentos ajenos: recuperación CLI, sin añadir un modelo por IP aparente. No hay roles, MFA ni recuperación por correo. La administración tiene publicación separada de la landing; su desactivación no apaga `/admin`.

Ver [operaciones §9](../operations.md#9-hub-privado-slice5) y [slice5](../slices/05-hub-privado-diagnosticos.md).
