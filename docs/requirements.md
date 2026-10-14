# DynDNSarr
## Documento de requisitos funcionales (PRD)
**Versión:** 0.3  
**Estado:** Alcance funcional definido  
**Categoría:** Homelab · Dynamic DNS · Network Monitoring · AI Automation

---

# 1. Visión del producto

DynDNSarr es una aplicación web ligera, autocontenida y configurable que permite monitorizar y administrar la conectividad de una red doméstica o homelab.

Su función principal es mantener actualizados los registros de DNS dinámico cuando cambia la dirección IP pública, proporcionando herramientas para supervisar la estabilidad de la conexión, consultar dispositivos de red, recibir alertas y administrar servicios.

En versiones posteriores, DynDNSarr evolucionará hacia una plataforma de gestión de conectividad compatible con agentes de inteligencia artificial mediante MCP (Model Context Protocol).

La aplicación tendrá identidad propia, representada por una pequeña mascota luminosa cuyo comportamiento reflejará el estado general del sistema.

## 1.1. Objetivos

1. Mantener actualizados los registros DDNS sin depender de otros equipos, como un NAS.
2. Detectar cambios de IP pública, errores DNS y problemas de conectividad.
3. Centralizar información de conectividad y dispositivos de red.
4. Facilitar operaciones remotas seguras, incluyendo Wake-on-LAN.
5. Integrarse con herramientas populares de homelab, comenzando por Pi-hole y Homarr.
6. Permitir la administración controlada mediante agentes de IA.
7. Facilitar la publicación temporal o permanente de aplicaciones mediante reverse proxies.

## 1.2. Principios de producto

- **Autonomía:** debe funcionar sin intervención del usuario después de configurarse.
- **Simplicidad:** la información importante debe entenderse de inmediato.
- **Bajo consumo:** adecuado para dispositivos como Raspberry Pi.
- **Modularidad funcional:** las integraciones opcionales no deben ser obligatorias.
- **Seguridad:** las acciones administrativas deben estar protegidas.
- **Extensibilidad:** inicialmente DonDominio y Caddy, pero preparado para ampliar compatibilidad.
- **Recuperación:** capacidad de reanudar automáticamente sus operaciones después de interrupciones.
- **Transparencia:** distinguir entre estados observados, inferidos y no comprobables.

# 2. Usuarios y modalidades de acceso

## 2.1. Visitante público

Accede a DynDNSarr desde Internet.

Puede consultar exclusivamente:

- Estado general del servicio.
- Indicador luminoso del estado.
- Fecha y hora de la última comprobación exitosa.
- Últimas cinco renovaciones DNS.
- Fecha, hora y únicamente los dos últimos dígitos de cada dirección IPv4 asociada a esas renovaciones.

No puede ver información privada de la infraestructura ni ejecutar operaciones.

La vista pública podrá deshabilitarse por completo.

## 2.2. Usuario administrador

Accede desde una red autorizada y mediante una sesión autenticada.

Puede consultar y administrar todos los recursos habilitados para su instalación, incluyendo dominios, dispositivos, alertas, historial e integraciones.

La procedencia desde la intranet no sustituye la autenticación para realizar operaciones administrativas.

## 2.3. Agente de inteligencia artificial

Disponible desde la versión 1.1.

Accede mediante MCP con credenciales y permisos específicos.

Los agentes pueden consultar datos y ejecutar acciones autorizadas. No reciben acceso administrativo general de forma predeterminada.

# 3. Gestión de Dynamic DNS

**Identificador:** DNS  
**Versión objetivo:** 1.0

## DNS-01. Detección de IP pública

DynDNSarr deberá consultar periódicamente la dirección IP pública utilizada por la conexión.

Requisitos:

- Frecuencia configurable.
- Comprobación inmediata al iniciar.
- Detección de cambios.
- Registro del momento de detección.
- Identificación de errores de consulta.
- Posibilidad de configurar más de una fuente de consulta.
- Tratamiento de resultados contradictorios sin asumir inmediatamente un cambio de IP.

El soporte inicial se centrará en IPv4. IPv6 se considera una ampliación posterior.

## DNS-02. Actualización automática

Cuando DynDNSarr detecte que la IP pública difiere del valor objetivo de un registro administrado, deberá iniciar su actualización.

Deberá:

- Ejecutar la actualización sin intervención.
- Evitar solicitudes repetidas innecesarias.
- Reintentar errores transitorios.
- Respetar límites del proveedor.
- Registrar el resultado.
- Verificar posteriormente la resolución DNS.
- Permitir una sincronización manual desde la administración.

## DNS-03. Proveedores DDNS

El proveedor inicial será **DonDominio**, utilizando DonDNS.

El producto deberá permitir conceptualmente incorporar otros proveedores en el futuro sin modificar las funcionalidades principales.

No se requiere implementar otros proveedores en la versión 1.0.

## DNS-04. Múltiples dominios

Se permitirá gestionar múltiples dominios y subdominios.

Cada registro tendrá:

- Nombre completo.
- Proveedor asociado.
- Estado habilitado/deshabilitado.
- IP objetivo.
- Última actualización.
- Última verificación.
- Estado operativo.
- Historial de eventos.

Los registros podrán agruparse por proveedor o dominio principal.

## DNS-05. Registros administrados y monitorizados

Se distinguirán dos modalidades:

- **Administrado:** DynDNSarr puede modificar el registro DNS.
- **Monitorizado:** DynDNSarr solamente consulta y verifica el registro.

## DNS-06. Historial DNS

Se registrarán tres tipos de eventos diferenciados:

1. Comprobación de IP o DNS.
2. Solicitud de actualización o renovación.
3. Cambio efectivo de dirección IP.

Cada evento relevante incluirá fecha, hora, registro afectado, resultado y detalles del cambio cuando corresponda.

El historial deberá persistir entre reinicios.

## DNS-07. Diagnóstico DNS

Será posible verificar:

- IP pública detectada.
- Dirección resuelta por el DNS configurado.
- Dirección resuelta mediante resolvers externos.
- Coincidencia con la IP esperada.
- Tiempo desde la última actualización correcta.
- Posibles discrepancias entre resolvers.
- Errores de resolución.

La interfaz distinguirá entre una actualización aceptada por el proveedor y una resolución DNS verificada.

# 4. Monitorización y estabilidad

**Identificador:** MON  
**Versión objetivo:** 1.0

## MON-01. Estados operativos

Cada dominio tendrá un estado propio. También existirá un estado general del producto.

| Estado | Significado |
|---|---|
| Verde | Comprobaciones correctas y DNS consistente |
| Amarillo | Cambio pendiente, comprobación fallida temporalmente o situación degradada |
| Rojo | Fallo confirmado o situación que requiere intervención |
| Gris | Información insuficiente o estado desconocido |

Las condiciones y los umbrales que determinan los estados deberán ser configurables.

Un problema opcional de Pi-hole no deberá provocar necesariamente que el DDNS general figure como fallido.

## MON-02. Uptime

La aplicación distinguirá:

- Tiempo de funcionamiento de DynDNSarr.
- Disponibilidad observada de Internet.
- Disponibilidad observada de resolución DNS.
- Tiempo desde el último cambio de IP.
- Duración de los incidentes detectados.
- Tiempo de recuperación después de incidentes.

No deberá afirmar disponibilidad continua durante períodos en los que DynDNSarr no pudo efectuar comprobaciones.

## MON-03. Estadísticas

Se ofrecerán indicadores de estabilidad para períodos configurables, inicialmente:

- Últimas 24 horas.
- Últimos 7 días.
- Últimos 30 días.

Los indicadores incluirán:

- Número de cambios de IP.
- Comprobaciones satisfactorias/fallidas.
- Incidentes registrados.
- Duración de incidentes observados.
- Tiempo entre cambios de IP.

## MON-04. Recuperación después de interrupciones

Cuando DynDNSarr vuelva a estar operativo deberá:

1. Recuperar su configuración e historial.
2. Comprobar conectividad.
3. Detectar la IP pública.
4. Verificar los dominios administrados.
5. Actualizar los registros necesarios.
6. Registrar la recuperación.
7. Evaluar las operaciones automáticas pendientes.

# 5. Historial y auditoría

**Identificador:** LOG  
**Versión objetivo:** 1.0

## LOG-01. Historial operativo

Se conservarán eventos de:

- Detección de cambios de IP.
- Actualizaciones DNS.
- Problemas de conectividad.
- Recuperaciones.
- Alertas enviadas.
- Operaciones Wake-on-LAN.
- Acciones administrativas.

## LOG-02. Consulta

El administrador podrá:

- Filtrar por fecha.
- Filtrar por dominio.
- Filtrar por tipo de evento.
- Consultar detalles de errores.
- Diferenciar acciones automáticas y manuales.

## LOG-03. Retención

El período de conservación será configurable.

El sistema evitará el crecimiento ilimitado del historial.

Los datos sensibles, como credenciales y tokens, nunca deberán aparecer en los registros.

# 6. Integración con Pi-hole

**Identificador:** PIH  
**Versión objetivo:** 1.0

La integración con Pi-hole será opcional.

## PIH-01. Dispositivos de red

Se presentará una lista de dispositivos conocidos, utilizando la información disponible mediante Pi-hole.

Datos previstos:

- Nombre o hostname.
- Dirección IP local.
- Dirección MAC, cuando esté disponible y el usuario esté autorizado.
- Información DHCP.
- Última actividad conocida.
- Estado estimado.

## PIH-02. Estados de dispositivos

Se distinguirán:

- Activo.
- Probablemente activo.
- Conocido.
- Sin actividad reciente.
- Desconocido.

La existencia de una concesión DHCP no será suficiente para afirmar que un dispositivo está conectado.

## PIH-03. Estado Pi-hole

Se mostrará:

- Disponibilidad de Pi-hole.
- Estado de la integración.
- Información básica del servicio DHCP, cuando esté disponible.
- Última consulta satisfactoria.

DynDNSarr no intentará reemplazar la administración DHCP o DNS de Pi-hole.

# 7. Wake-on-LAN

**Identificador:** WOL  
**Versión objetivo:** 1.0

## WOL-01. Dispositivos configurados

El administrador podrá registrar dispositivos compatibles, incluyendo inicialmente su NAS.

Cada dispositivo tendrá:

- Nombre.
- Dirección MAC.
- Dirección IP esperada.
- Estado estimado.
- Permiso de encendido manual.
- Política de encendido automático.

## WOL-02. Encendido manual

Un administrador podrá solicitar el encendido de un equipo autorizado.

DynDNSarr deberá diferenciar entre:

- Orden enviada.
- Dispositivo en proceso de inicio.
- Dispositivo confirmado como accesible.
- Encendido no confirmado.

## WOL-03. Encendido automático

Será posible configurar que un dispositivo, como el NAS, se encienda automáticamente después de una recuperación del sistema.

Condiciones funcionales:

- Función habilitada explícitamente por dispositivo.
- Verificación previa de su estado, cuando sea posible.
- Evitar solicitudes innecesarias o repetidas.
- Registrar el resultado.
- No apagar dispositivos.
- No reiniciar dispositivos de manera forzada.

La función estará condicionada a la compatibilidad Wake-on-LAN y a las características del hardware.

# 8. Alertas y notificaciones

**Identificador:** ALT  
**Versión objetivo:** 1.0

## ALT-01. Canales

Canales iniciales:

- Correo electrónico.
- Webhooks HTTP/HTTPS genéricos.
- Webhooks compatibles con Slack.

Telegram queda fuera del alcance inicial.

## ALT-02. Eventos notificables

Se permitirá configurar alertas para:

- Cambio de IP pública.
- Actualización DDNS fallida.
- DNS inconsistente.
- Interrupción de conectividad detectada.
- Recuperación de servicio.
- Error persistente de una integración.
- Resultado de encendido automático del NAS.

## ALT-03. Reglas

El administrador podrá configurar:

- Eventos que generan notificaciones.
- Canal de destino.
- Nivel de severidad.
- Umbral previo a una alerta.
- Supresión de notificaciones repetidas.
- Notificaciones de recuperación.

## ALT-04. Pruebas

Será posible enviar notificaciones de prueba para verificar la configuración.

No se garantizará recibir notificaciones de una caída completa si DynDNSarr no puede ejecutarse o no dispone de conectividad.

# 9. Interfaz web y seguridad

**Identificador:** WEB  
**Versión objetivo:** 1.0

## WEB-01. Vista pública

La vista pública estará diseñada para consulta rápida.

Mostrará:

- Estado general.
- Mascota con estado luminoso.
- Última comprobación satisfactoria.
- Últimas cinco renovaciones.
- Fecha y hora de esas renovaciones.
- Dos últimos dígitos de la IP IPv4 relacionada.

No incluirá:

- Dirección IP pública completa.
- Direcciones internas.
- Nombres de dispositivos.
- Configuraciones.
- Logs detallados.
- Operaciones administrativas.
- Credenciales.

La publicación de esta vista será opcional.

## WEB-02. Vista privada

Ofrecerá acceso a:

- Estado general.
- Dominios.
- Historial.
- Diagnósticos.
- Pi-hole.
- Dispositivos.
- Wake-on-LAN.
- Alertas.
- Configuración.
- Integraciones.

## WEB-03. Autenticación

El acceso a funciones administrativas requerirá iniciar sesión.

Requisitos:

- Protección de operaciones sensibles.
- Gestión segura de sesiones.
- Cierre de sesión.
- Protección contra intentos reiterados de autenticación.
- No exposición de credenciales.
- Protección de operaciones de modificación.

## WEB-04. Detección de acceso

DynDNSarr podrá diferenciar accesos locales y externos para seleccionar la experiencia apropiada.

No deberá utilizar la procedencia aparente de una solicitud como única prueba de autorización.

La clasificación del acceso deberá contemplar la existencia de proxies inversos.

# 10. Integración con Homarr

**Identificador:** HOM  
**Versión objetivo:** 1.0

DynDNSarr ofrecerá una integración que permita consultar su estado desde Homarr.

Información mínima:

- Estado general.
- Última comprobación exitosa.
- Último cambio de IP.
- Acceso a la aplicación.

El widget podrá representar el estado mediante la mascota de DynDNSarr.

Las funciones administrativas integradas en Homarr estarán sujetas a los mismos permisos que la aplicación.

La integración no será obligatoria para utilizar DynDNSarr.

# 11. API de administración y consulta

**Identificador:** API  
**Versión objetivo:** 1.0

DynDNSarr tendrá capacidades de consulta y operación reutilizables por sus propias interfaces y futuras integraciones.

Operaciones funcionales:

- Consultar estado.
- Listar dominios.
- Consultar historial.
- Ejecutar comprobación DNS.
- Solicitar actualización DDNS.
- Consultar dispositivos.
- Ejecutar Wake-on-LAN.
- Consultar incidentes.
- Gestionar alertas y configuración autorizada.

La API deberá respetar autenticación, permisos, validación y auditoría.

Los detalles técnicos de implementación se definirán en arquitectura.

# 12. Integración MCP

**Identificador:** MCP  
**Versión objetivo:** 1.1

DynDNSarr permitirá que agentes como Codex y Claude interactúen con el producto mediante Model Context Protocol.

## MCP-01. Herramientas de consulta

Capacidades previstas:

- Consultar conectividad.
- Listar dominios.
- Consultar historial DNS.
- Consultar dispositivos.
- Consultar servicios publicados.
- Consultar salud de servicios.

## MCP-02. Herramientas operativas

Capacidades previstas:

- Comprobar DNS.
- Solicitar actualización DDNS.
- Encender dispositivos autorizados.
- Verificar servicios publicados.
- Crear, modificar y eliminar publicaciones autorizadas.

## MCP-03. Modelo de permisos

Se adoptará un modelo **scoped**, es decir, autonomía dentro de permisos y recursos previamente autorizados.

Se distinguirán capacidades de:

1. Solo lectura.
2. Operación.
3. Administración y publicación.

Los permisos podrán limitarse por:

- Agente o cliente.
- Tipo de operación.
- Dominio o subdominio.
- Dispositivo.
- Destino de publicación.
- Rango de puertos autorizados.

Un agente no podrá ampliar sus propios permisos.

## MCP-04. Auditoría

Toda operación modificadora realizada por un agente deberá registrar:

- Identidad del agente autorizado.
- Acción solicitada.
- Recurso afectado.
- Momento de ejecución.
- Resultado.
- Errores o rechazo de autorización.

## MCP-05. Confirmación y seguridad

Las operaciones dentro de permisos explícitamente delegados podrán ejecutarse sin confirmación humana adicional.

Las operaciones fuera de esos permisos deberán rechazarse o requerir un proceso de autorización independiente.

# 13. Gestión de reverse proxy

**Identificador:** GAT  
**Versión objetivo:** 1.1

El primer reverse proxy compatible será **Caddy**.

El producto permitirá integrar otros reverse proxies en versiones posteriores sin comprometer el funcionamiento de Caddy.

## GAT-01. Publicaciones

Se permitirá registrar publicaciones de aplicaciones o servicios mediante un dominio o subdominio.

Cada publicación tendrá:

- Nombre.
- Dominio público.
- Destino interno autorizado.
- Puerto.
- Reverse proxy asociado.
- Modalidad pública o privada.
- Estado.
- Política HTTPS.
- Creador.
- Fecha de creación.
- Expiración opcional.

## GAT-02. Operaciones

Se permitirá:

- Crear publicaciones.
- Modificar destinos.
- Deshabilitar publicaciones.
- Eliminar publicaciones.
- Consultar el estado de las publicaciones.
- Comprobar la accesibilidad.
- Consultar errores.

## GAT-03. Publicaciones temporales

Una publicación podrá tener tiempo de expiración.

Ejemplos:

- Dos horas.
- Un día.
- Una fecha específica.
- Sin expiración.

Al expirar, DynDNSarr deberá retirar o deshabilitar la publicación según la política configurada.

## GAT-04. Validaciones

Antes de publicar un servicio se deberán comprobar, cuando corresponda:

- Permisos del solicitante.
- Dominio autorizado.
- Destino permitido.
- Puerto autorizado.
- Conflictos con publicaciones existentes.
- Validez de la configuración propuesta.

## GAT-05. Reversión

Si una modificación falla o produce una configuración inválida, DynDNSarr deberá permitir recuperar el último estado válido.

Se conservará un historial de cambios administrativos.

## GAT-06. DNS y publicación

Se distinguirá entre:

- Administrar el registro DNS.
- Configurar el enrutamiento del reverse proxy.
- Verificar la accesibilidad del servicio publicado.

No se modificará DNS innecesariamente cuando el dominio ya esté correctamente dirigido.

## GAT-07. Alcance de administración

DynDNSarr no intentará reemplazar todas las funcionalidades del reverse proxy.

Se centrará en publicaciones habituales HTTP/HTTPS, sus destinos, estados y ciclos de vida.

La compatibilidad con configuraciones avanzadas se evaluará en versiones posteriores.

# 14. Seguridad transversal

**Identificador:** SEC  
**Versión objetivo:** Aplicable a todas las versiones

Requisitos generales:

- Las credenciales de proveedores DNS deberán permanecer protegidas.
- Los visitantes públicos no podrán ejecutar acciones administrativas.
- Las herramientas de IA tendrán permisos explícitos y limitados.
- Las integraciones externas no recibirán acceso irrestricto a la red.
- Las publicaciones deberán estar limitadas a dominios y destinos autorizados.
- Se bloqueará la publicación de destinos sensibles no expresamente permitidos.
- Las modificaciones deberán ser trazables.
- Las operaciones críticas deberán contar con validación previa.
- La información pública será mínima y configurable.
- Deberá existir una forma de revocar las credenciales y autorizaciones concedidas.

# 15. Configuración y mantenimiento

**Identificador:** CFG  
**Versión objetivo:** 1.0

## CFG-01. Configuración inicial

El usuario deberá poder configurar:

- Proveedor DNS.
- Credenciales.
- Dominios.
- Frecuencia de comprobación.
- Preferencias de monitorización.
- Integración con Pi-hole.
- Dispositivos Wake-on-LAN.
- Canales de alerta.
- Acceso público.
- Credenciales administrativas.

## CFG-02. Configuración opcional

La aplicación deberá funcionar sin Pi-hole, Homarr, Wake-on-LAN, MCP o reverse proxy.

Un error de integración opcional no deberá impedir las actualizaciones DDNS.

## CFG-03. Persistencia

Configuración e historial deberán sobrevivir a reinicios y actualizaciones.

## CFG-04. Operaciones de mantenimiento

El administrador podrá:

- Consultar el estado de los componentes.
- Comprobar integraciones.
- Revisar errores.
- Probar conexiones.
- Modificar la configuración.
- Exportar datos relevantes de diagnóstico.

# 16. Requisitos no funcionales de producto

Sin determinar tecnologías o arquitectura, se establecen las siguientes expectativas:

| Área | Requisito |
|---|---|
| Plataforma | Compatible con instalaciones homelab, especialmente Raspberry Pi |
| Recursos | Consumo reducido de CPU, memoria y almacenamiento |
| Autonomía | Recuperación y sincronización automáticas |
| Fiabilidad | Errores de una integración no deben bloquear las demás |
| Seguridad | Autenticación, permisos y auditoría |
| Usabilidad | Información comprensible para usuarios no expertos |
| Modularidad | Integraciones opcionales y ampliables |
| Persistencia | Conservación de datos entre reinicios |
| Internacionalización | Preparado para varios idiomas |
| Accesibilidad | Los estados no dependerán exclusivamente del color |

# 17. Roadmap funcional

## Versión 1.0 — DynDNSarr Core

Primera versión funcional de la aplicación.

Incluye:

- DonDominio.
- Múltiples dominios.
- Actualización DDNS automática.
- Diagnóstico DNS.
- Monitorización y estabilidad.
- Historial de eventos.
- Integración Pi-hole.
- Dispositivos conocidos.
- Wake-on-LAN manual y automático.
- Alertas por correo y webhooks.
- Vista pública y privada.
- Autenticación.
- Integración Homarr.
- Capacidades API para consulta y operaciones.
- Mascota e indicador de estados.

**Objetivo:** sustituir al NAS como responsable del DDNS y proporcionar un panel operativo completo.

## Versión 1.1 — DynDNSarr AI & Gateway

Incluye:

- Servidor MCP.
- Permisos específicos para agentes.
- Operaciones de IA auditadas.
- Integración con Caddy.
- Gestión de publicaciones.
- Publicaciones temporales.
- Validación de cambios.
- Recuperación de configuraciones válidas.
- Automatización de entornos de desarrollo.

**Objetivo:** permitir que herramientas como Codex y Claude gestionen recursos de conectividad y publiquen aplicaciones dentro de límites previamente autorizados.

## Versión 1.2 y posteriores — Extensibilidad

Posibles ampliaciones:

- Proveedores DDNS adicionales.
- Otros reverse proxies.
- IPv6.
- Monitorización externa.
- Reglas de automatización más avanzadas.
- Integraciones con otros servicios de homelab.
- Gestión más completa de certificados y publicaciones.

Estas capacidades no se consideran comprometidas para la primera versión.

# 18. Exclusiones explícitas

DynDNSarr no pretende convertirse en:

- Un servidor DHCP.
- Un sustituto de Pi-hole.
- Un gestor general de contenedores.
- Una plataforma de despliegue de aplicaciones.
- Un firewall completo.
- Un sistema de gestión de VPN.
- Un sustituto integral de Caddy.
- Un sistema de monitorización de infraestructura empresarial.
- Un administrador remoto irrestricto de dispositivos.

Estas exclusiones mantienen acotado el producto.

# 19. Criterios generales de aceptación

La versión 1.0 podrá considerarse funcionalmente completa cuando:

1. Actualice automáticamente un dominio DonDominio después de un cambio de IP pública.
2. Mantenga múltiples registros con estados independientes.
3. Detecte e informe discrepancias DNS.
4. Conserve historial y configuración entre reinicios.
5. Muestre una vista pública sin información privada.
6. Proteja las funcionalidades administrativas mediante autenticación.
7. Permita consultar dispositivos mediante Pi-hole.
8. Permita enviar Wake-on-LAN y ejecutar la política de encendido automático.
9. Envíe alertas por correo y webhooks.
10. Presente el estado mediante Homarr.
11. Se recupere automáticamente de reinicios o interrupciones del servicio, siempre que el dispositivo anfitrión vuelva a encenderse.

La versión 1.1 podrá considerarse funcionalmente completa cuando, adicionalmente:

1. Un agente MCP autorizado pueda consultar la información de DynDNSarr.
2. Un agente pueda actualizar un registro DNS dentro de sus permisos.
3. Un agente pueda crear una publicación Caddy dentro de dominios y destinos previamente autorizados.
4. Una publicación temporal expire correctamente.
5. Una modificación inválida sea rechazada sin comprometer las publicaciones existentes.
6. Las acciones de los agentes queden auditadas.
7. Los permisos puedan revocarse.

# 20. Resumen ejecutivo

DynDNSarr es un guardián inteligente de conectividad para homelabs.

Comienza resolviendo un problema concreto: mantener los dominios dinámicos actualizados aunque otros equipos de la red estén apagados.

La primera versión amplía esa función mediante monitorización, historial, Pi-hole, Wake-on-LAN, alertas e integración Homarr.

La siguiente versión permite que los agentes de IA consulten el estado de la red y administren dominios y publicaciones mediante MCP, con permisos delimitados.

El resultado buscado es una aplicación pequeña, autónoma, segura y extensible, con una personalidad visual reconocible.

**Fin del documento de requisitos funcionales v0.3.**