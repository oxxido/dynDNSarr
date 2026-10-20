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
