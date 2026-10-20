# ADR-0008 — Animación de la mascota: cara SVG superpuesta, flotación, sombra y burbujas

**Fecha:** 2026-10-20

## Contexto

[architecture.md](../architecture.md) §0.7 y §8 y [ADR-0007](0007-landing-publica.md) (A7) fijaban «no animar ojos ni boca»: los PNG de Dyno vienen sin rostro y una cara visible exigiría otros assets y otro slice. Con los PNG ya en el repositorio, el responsable del producto pidió una cara con ojos que sigan al ratón y un ambiente marino. Esto revierte esa restricción, pero no la de «no reconstruir la mascota con SVG».

## Decisión

1. **Cara SVG superpuesta (A7).** Cada mascota lleva un `<svg viewBox="0 0 1254 1254" aria-hidden="true">` encima del PNG original (macro `face` de `templates/public/index.html.j2`). El PNG no se modifica y la mascota sigue siendo el PNG. Las coordenadas están en el espacio de la imagen, así que la cara escala con ella.
2. **Ojos.** Las pupilas siguen al puntero (`pointermove` y `pointerdown`, así también al tocar en móvil), cada ojo desde su propio centro, con desplazamiento máximo y saturación por distancia. El JS usa variables CSS (`--x`, `--y`) fijadas por CSSOM: compatible con la CSP con nonce, sin atributos `style=`. Los ojos parpadean por CSS.
3. **Boca por estado.** Sonrisa (`healthy`), boca ondulada (`degraded`), ceño (`critical`) y línea recta (`unknown`); los dos mini-Dynos usan siempre naranja y rojo. «Respira» con una escala suave.
4. **Ambiente.** La mascota flota (`bob`, ±3,5 % de su altura, 5,5 s) y su cuerpo (PNG y cara juntos) se mueve sobre un contenedor fijo que lleva la **sombra** elíptica en la base, que se encoge y aclara cuando el cuerpo sube. Burbujas CSS (`.bubbles`, 14 `<i>` con posiciones y tiempos en reglas `nth-child`) suben por detrás del hero. Todo es decorativo (`aria-hidden`) y no depende de datos.
5. **Movimiento reducido.** Con `prefers-reduced-motion: reduce` no hay parpadeo, respiración, flotación, sombra animada ni burbujas, y el JS de seguimiento no se registra (pupilas centradas).

## Consecuencias

- Si se cambian los PNG por otros con otra proporción o posición del cuerpo, hay que reajustar las coordenadas de la cara.
- Los PNG pesan ~1,5 MB cada uno (~4,5 MB por carga de la página): **optimizarlos** (redimensionar a ~800 px) queda pendiente.
- La página sigue sin refrescarse sola; las animaciones no cambian el estado mostrado.
- Si en el futuro se aportan sprites con cara, esta superposición se retira.
