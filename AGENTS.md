# Instrucciones de trabajo — DynDNSarr

## Forma de trabajar:

1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

State your assumptions explicitly. If uncertain, ask.
If multiple interpretations exist, present them - don't pick silently.
If a simpler approach exists, say so. Push back when warranted.
If something is unclear, stop. Name what's confusing. Ask.

2. Simplicity First
Minimum code that solves the problem. Nothing speculative.

No features beyond what was asked.
No abstractions for single-use code.
No "flexibility" or "configurability" that wasn't requested.
No error handling for impossible scenarios.
If you write 200 lines and it could be 50, rewrite it.
Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:

Don't "improve" adjacent code, comments, or formatting.
Don't refactor things that aren't broken.
Match existing style, even if you'd do it differently.
If you notice unrelated dead code, mention it - don't delete it.
When your changes create orphans:

Remove imports/variables/functions that YOUR changes made unused.
Don't remove pre-existing dead code unless asked.
The test: Every changed line should trace directly to the user's request.

4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

"Add validation" → "Write tests for invalid inputs, then make them pass"
"Fix the bug" → "Write a test that reproduces it, then make it pass"
"Refactor X" → "Ensure tests pass before and after"

## Leer según la tarea

- Producto y alcance: [requirements.md](docs/requirements.md).
- Límites de módulos: [architecture.md](docs/architecture.md).
- Roadmap técnico: [plan.md](plan.md).
- Reglas de código: [docs/conventions.md](docs/conventions.md).
- Slice activo y proceso: [docs/slices/README.md](docs/slices/README.md).
- Decisiones duraderas: [docs/decisions/README.md](docs/decisions/README.md).
- NO LEER NADA dentro de [docs/deprecated/]


Los HTML de `/design_docs/` son referencias visuales, no código de producción. se lee cuando se necesita implementar un componente grafico nuevo

## Alcance de lectura

No leer todo el directorio. Leer solo lo que necesita la tarea; para `docs/architecture.md`, solo las secciones indicadas.

| Tarea | Leer | No leer |
|---|---|---|
| Cualquier slice | AGENTS.md, `plan.md`, el archivo del slice, `docs/conventions.md` | — |
| Agent (Raspberry): DDNS, DNS, monitorización | architecture §0, §2, §3, §5, §6, §7, §8, §11 | `design_docs/`, §9 |
| Contrato Agent–Hub | architecture §3, §5, §6 | `design_docs/` |
| Hub: UI pública/privada | architecture §9 y `design_docs/` | §7 |
| Integraciones (Pi-hole, WoL, alertas, Homarr) | architecture §10, §11, §13 | `design_docs/` |
| Despliegue y bootstrap | architecture §2, §4, §11, §12 | `design_docs/` |
| Alcance de producto | `docs/requirements.md` solo el capítulo afectado | resto |

`docs/requirements.md` es referencia de alcance; ante conflicto prevalece `docs/architecture.md` (ADR-0001).

## Estado y comandos

ESTADO.md

## Trabajo por slices y Git

1. Planificar solo el siguiente slice con detalle en `docs/slices/NN-nombre.md`. Usar la plantilla y actualizar el índice. El roadmap no constituye un slice listo.
2. Una rama y un PR por slice por defecto. Crear la rama desde `master` actualizado; el plan detallado y la implementación pertenecen a esa rama y PR.
3. No implementar un slice cuando el encargo se limite a planificarlo. Al recibir el encargo de ejecución, completar alcance, pruebas y documentación del slice.
4. Hacer commits pequeños y coherentes, revisar el diff y los archivos incluidos. No incluir secretos, documentos reales ni cambios ajenos. No reescribir historial remoto ni descartar trabajo del usuario.
5. El flujo de entrega acordado para ejecutar un slice incluye commits, push de su rama y apertura de PR. Si falta remoto o acceso, dejar trabajo local verificable y comunicar el requisito faltante. No inventar un destino ni crear un repositorio remoto por suposición.
6. PR contra `master`: problema, comportamiento resultante, alcance, validación real y migraciones/riesgos relevantes. Atender revisión y CI. No confundir push o PR abierto con merge.
7. Fusionar cuando se haya autorizado el merge y se cumplan los controles del repositorio. No inferir autorización para fusionar del permiso para abrir el PR. Tras el merge, partir de `master` actualizado para la siguiente ronda.
8. Registrar rama, PR, estado y evidencia en el slice. Estados: pendiente, planificado, en implementación, en revisión, fusionado. Marcar fusionado solo tras verificarlo.

Excepción de arranque: la base documental se commitea directamente en `master` (creada al inicializar Git). A partir de slice1 se trabaja por ramas y PR. No simular pushes o PR mientras no exista remoto.

## REGLAS
- NUNCA modificar AGENTS.md. De ser necesario, pedir permiso al usuario y mostrar que y como se va a modificar.
• PROHIBIDO ejecutar comandos destructivos globales: Bajo ninguna circunstancia ejecutes comandos de borrado masivo o formateo que apunten fuera de la carpeta del proyecto. Queda estrictamente prohibido el uso de:
	• rm -rf / o cualquier variante con sudo.
	• dd, mkfs, fdisk, parted o cualquier herramienta de manipulación de discos y particiones.
	• Comandos dirigidos a directorios del sistema como /etc, /var, /usr, /bin, o /opt.
• PROHIBIDO el uso de flags de borrado forzado sin confirmación: No utilices rm -rf o rm -f dentro del proyecto de forma automatizada o recursiva sobre directorios raíz del repositorio (., *), a menos que sea para limpiar una carpeta específica de caché explícitamente definida (ej. rm -rf ./dist o rm -rf ./node_modules).
• PROHIBIDO modificar configuraciones del sistema operativo: Está prohibido alterar archivos de configuración globales (/etc/hosts, /etc/resolv.conf), instalar daemons del sistema, modificar reglas del firewall (iptables, ufw) o alterar variables de entorno globales del sistema de ejecución.
• PROHIBIDO descargar o ejecutar scripts remotos no verificados: No utilices curl o wget para descargar código, binarios o scripts de instalación y ejecutarlos directamente mediante sh o bash (ej. curl ... | sh está terminantemente prohibido).
• PROHIBIDO eludir los límites del entorno de ejecución: Si detectas que una tarea requiere permisos de superusuario (sudo) para interactuar con el hardware, los discos del sistema o procesos de red restringidos, detén la ejecución inmediatamente y solicita la intervención manual de un humano.

PROHIBIDO TRABAJAR FUERA DE LA CARPETA DE CODE/dynDNSarr, bajo ningun concepto, no se pueden ejecutar comandos de crear carpeta ni archivos ni leer nada fuera de esta carpeta.

TODO COMANDO que afecte de forma global al servidor, debe ser autorizado por usuario.

