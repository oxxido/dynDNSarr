# ADR-0001 — La arquitectura prevalece sobre el PRD

**Fecha:** 2026-10-14

## Contexto

[requirements.md](../requirements.md) (PRD v0.3) se escribió primero y no contempla la separación Agent + Hub. [architecture.md](../architecture.md) (handoff v0.6) la define y es posterior.

## Decisión

Ante cualquier diferencia, prevalece `architecture.md`. El PRD sigue siendo la referencia de alcance de producto (qué y para quién); la arquitectura decide cómo y en qué orden.

Reconciliaciones ya conocidas:

- **Estados:** Verde/Amarillo/Rojo/Gris (PRD) = `healthy`/`degraded`/`critical`/`unknown` (arquitectura). El estado no puede depender solo del color.
- **IPv6:** fuera de v1.0; cualquier mención en la maqueta se oculta o se muestra como "No disponible".
- **Dominios en la vista pública:** política conservadora hasta resolver A5.
- **Autocontenida (PRD §1):** se interpreta como Agent autónomo; el Hub es opcional para el DDNS.

## Consecuencias

Si el PRD queda desfasado de forma relevante, se actualiza o se anota aquí; no se corrige la arquitectura para ajustarla al PRD.
