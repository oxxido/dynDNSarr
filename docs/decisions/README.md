# Decisiones duraderas (ADR)

Una decisión que debe sobrevivir a un slice se registra aquí: `NNNN-titulo-corto.md`, con contexto, decisión y consecuencias. Las decisiones propias de un slice viven en su archivo de slice.

| ADR | Decisión |
|---|---|
| [0001](0001-arquitectura-prevalece-sobre-prd.md) | La arquitectura prevalece sobre el PRD |
| [0002](0002-python-3-11.md) | Python 3.11 como versión mínima |
| [0003](0003-motor-ddns-observacion.md) | Configuración TOML, scheduler asyncio y consenso de IP del motor DDNS |
| [0004](0004-actualizacion-ddns-dondominio.md) | API DonDominio, credenciales, backoff y verificación de la actualización `managed` |
| [0005](0005-enlace-agent-hub.md) | Enlace Agent → Hub: proxy TLS verificado, registro manual, token por agente, contrato y outbox at-least-once |
| [0006](0006-control-hub-agent.md) | Control Hub → Agent: TLS propio del Agent, token Bearer, configuración versionada y comandos idempotentes |
| [0007](0007-landing-publica.md) | Landing pública: proyección sin IP completa, estado derivado al leer, historial observado y publicación con CSP |
| [0008](0008-animacion-mascota.md) | Animación de la mascota: cara SVG superpuesta, flotación, sombra y burbujas |
| [0009](0009-administracion-web.md) | Administrador local, sesiones, CSRF, auditoría e idempotencia web |
| [0010](0010-monitorizacion-privada.md) | Diagnóstico privado, política común de estados y uptime por duración observada |
| [0011](0011-integraciones-v1.md) | Pi-hole v6, WoL por política, alertas persistentes, Homarr y retención/exportación |
| [0012](0012-servicios-mcp-caddy.md) | API scoped, MCP stdio, comandos DNS por registro y publicaciones Caddy con revisiones/expiración |
| [0013](0013-instalacion-emparejamiento.md) | Instalación asistida, emparejamiento Agent ↔ Hub por código, puerto de enlace HTTPS y setup web del administrador |
