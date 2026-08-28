# WebSockets: validador de Origin (Medio #15)

Fecha: 2026-08-28
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Medio #15.

## Problema

Los dos WebSockets del proyecto (`/ws/auth/`, pareo de TV; `/ws/device/`, dispositivos vinculados) no tenían ningún validador de `Origin` -- cualquier página web, de cualquier dominio, podía abrir una conexión contra ellos. El escenario real que esto protege: una página maliciosa corriendo en el navegador de la víctima que intenta abrir un WebSocket contra este backend.

## Por qué no se usó el validador estándar de Channels tal cual

`channels.security.websocket.AllowedHostsOriginValidator` es la solución "de manual", pero se descartó tal cual: usa `settings.ALLOWED_HOSTS` (hosts concretos como `backend.wind.do`, sin `"*"`) como lista de orígenes permitidos, y el `OriginValidator` de Channels en el que se apoya **rechaza cualquier conexión que no traiga header `Origin`**, salvo que `"*"` esté en esa lista (confirmado leyendo el código fuente instalado de `channels.security.websocket`).

El problema: los clientes nativos (apps Android/iOS, Smart TV con librería WebSocket nativa, no un navegador embebido) normalmente **no mandan header `Origin`** -- es un concepto de navegador. Aplicado tal cual, esto habría cortado en producción el pareo de TV y "dispositivos vinculados" para cualquier cliente que no sea un navegador web, sin que nadie lo pidiera ni lo esperara.

## Solución implementada

`wind/utils/ws_origin_validator.py`, clase `NativeAwareOriginValidator` (subclase de `OriginValidator` de Channels) + factory `native_aware_origin_validator()`:

- Si la conexión **no** trae header `Origin` → se permite (caso normal de un cliente nativo).
- Si **sí** trae `Origin` → se exige que esté en `settings.CORS_ALLOWED_ORIGINS` -- se reutiliza la misma lista que ya autoriza qué webs pueden llamar la API REST vía CORS, en vez de mantener una lista nueva en paralelo.

`panaccess_wind_integration/asgi.py`: se envuelve todo `AuthMiddlewareStack(URLRouter(wind.routing.websocket_urlpatterns))` con `native_aware_origin_validator(...)` -- queda como la capa más externa, así rechaza un origen no autorizado antes de gastar nada en autenticación.

## Verificación realizada

- `py_compile` sobre `wind/utils/ws_origin_validator.py` y `panaccess_wind_integration/asgi.py`: sin errores.
- `manage.py check`: sin problemas.
- Verificación funcional directa del validador (instanciado con `settings.CORS_ALLOWED_ORIGINS` real):
  - Sin header `Origin` (cliente nativo) → permite.
  - `Origin` de la lista (`http://localhost:3000`) → permite.
  - `Origin` ajeno (`https://evil-attacker.example`) → rechaza.
- Pendiente (no bloqueante, recomendado antes de dar por cerrado del todo): probar contra producción real que el pareo de TV y `/ws/device/` siguen conectando desde appVideo/apps nativas después de este cambio, ya que toca el punto de entrada de ambos sockets.

## Nota aparte encontrada durante la verificación

`CORS_ALLOWED_ORIGINS` en `.env` tiene una entrada con el esquema mal escrito: `hhtp://192.168.1.183:3000` (debería ser `http://`). No se corrigió en este cambio (no es parte del hallazgo Medio #15 ni afecta su funcionamiento -- esa entrada en particular simplemente nunca va a matchear nada, ni para CORS ni para este validador), pero vale la pena que se revise si esa IP de desarrollo todavía se usa.

## Estado

Medio #15 queda **resuelto**. De paso, en esta misma revisión se corrigieron dos filas de la tabla que ya estaban resueltas mucho antes en el código pero no reflejadas: Medio #18 (reset token, BD ya es la fuente de verdad) y se reconfirmó Medio #16 (fingerprint evadible) como limitación aceptada sin acción pendiente.
