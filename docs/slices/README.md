# Slices

Un slice es la unidad de trabajo: una rama y un PR. El orden y el alcance general están en [plan.md](../../plan.md); aquí vive el proceso y el índice de estado.

## Reglas

- Solo se detalla el siguiente slice. El roadmap no es un slice listo.
- Archivo: `docs/slices/NN-nombre.md` (p. ej. `01-bootstrap.md`), creado desde [_template.md](_template.md) en la rama del slice.
- Rama desde `main` actualizado: `slice/NN-nombre`.
- Planificar un slice no implica implementarlo; la ejecución se encarga por separado.
- Estados: `pendiente`, `planificado`, `en implementación`, `en revisión`, `fusionado`. Se marca `fusionado` solo tras verificarlo.
- Al cambiar de estado, actualizar el índice y registrar rama, PR y evidencia en el archivo del slice.

## Índice

| Slice | Fase | Nombre | Estado | Rama | PR |
|---|---|---|---|---|---|
| 1 | 0 | Monorepo y entorno ([plan](01-bootstrap.md)) | fusionado | `slice/01-bootstrap` | #1 |
| 2a | 1 | Observación de IP y DNS ([plan](02a-observacion-ip-dns.md)) | fusionado | `slice/02a-observacion-ip-dns` | #2 |
| 2b | 1 | Actualización DDNS `managed` (DonDominio) ([plan](02b-actualizacion-ddns-managed.md)) | fusionado | `slice/02b-actualizacion-ddns-managed` | #3 |
| 3a | 2 | Enlace seguro y entrega de eventos, Agent → Hub ([plan](03a-enlace-seguro-eventos.md)) | fusionado | `slice/03a-enlace-seguro-eventos` | #4 |
| 3b | 2 | Control Hub → Agent: estado, configuración versionada y comandos ([plan](03b-control-hub-agent.md)) | en revisión | `slice/03b-control-hub-agent` | #5 |
| 4 | 3 | Landing pública real | pendiente | — | — |
| 5 | 4 | Hub privado y diagnósticos | pendiente | — | — |
| 6 | 5 | Integraciones y release v1.0 | pendiente | — | — |
| 7 | 6 | v1.1 (MCP y Caddy) | pendiente | — | — |
