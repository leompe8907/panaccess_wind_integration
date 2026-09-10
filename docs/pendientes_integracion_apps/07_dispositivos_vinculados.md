# Dispositivos vinculados (sesiones de la cuenta: listar, revocar, notificar)

Estado: backend listo. Implementado en appVideo con 4 gaps ya corregidos y 2 sin resolver (documentados abajo). iOS/Android nativo debe implementar el mismo protocolo desde cero.
Referencia: `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 4 (protocolo completo, común a TV/mobile/web) y 3.1 (estado real en appVideo).

No confundir con `06_vincular_dispositivo.md`: esto no es "autorizar una TV nueva", es el registro y listado de **cualquier** dispositivo (TV, mobile, web) que se autenticó contra la cuenta, para que el usuario pueda ver "dónde tengo sesión iniciada" y cerrarla remotamente -- como en Netflix o Google.

## Prerrequisito

Este protocolo usa el mismo JWT que el resto de la API (`/api/auth/login/` o login social). Un dispositivo que solo hace pareo de TV (`06_vincular_dispositivo.md`) y nunca llama a `/api/auth/login/` directamente queda fuera de esto sin que rompa nada -- es opcional para TV, pero mobile normalmente sí lo implementa porque ya tiene el JWT de todos modos.

## 1. Registro por WebSocket

`ws/device/?token=<jwt de access>` (el JWT va en el query param, no hay forma de mandar headers en el handshake WS desde algunas plataformas). Cierres: **4001** (JWT inválido/expirado, o límite de conexiones excedido), **4004** (JWT válido pero no se pudo resolver `subscriber_code`).

Mensaje para registrarse: `{"type":"register_device","device_type":"...","device_model":"...","device_token":"<opcional, el guardado la vez anterior>"}`. `device_type` recomendado: `iOS`, `android`, `web`, `lg`, `samsung`.

Respuesta: `{"type":"device_registered","id":<int>,"device_token":"<token>","is_new":true|false}`. **Guardar `device_token` e `id` de forma persistente y segura** (Keychain en iOS, Keystore/EncryptedSharedPreferences en Android) y reenviar `device_token` en la próxima conexión para refrescar el registro en vez de crear uno nuevo. El `id` es el mismo que aparece en `GET /wind/devices/` -- sirve para marcar "este dispositivo" en la lista propia sin heurísticas.

Límite: 20 dispositivos **nuevos** por hora por suscriptor (refrescar uno existente no cuenta). Si el `device_token` enviado no es válido (revocado o de otra cuenta): `{"type":"error","code":"device_token_invalid",...}` y cierre 1011 -- tratar igual que una revocación (borrar el token local, registrarse de nuevo sin él).

`ping`/`pong`: el servidor manda `{"type":"ping"}` cada 30s, responder `{"type":"pong"}` (o se cierra a los 180s de inactividad).

## 2. Listar y revocar (REST, JWT)

`GET /wind/devices/` → `{"devices": [{"id","device_type","device_model","first_seen_at","last_seen_at","client_ip","country","city"}, ...]}` (el `device_token` nunca se expone). `country`/`city` son ubicación aproximada resuelta desde la IP, puramente informativa -- pueden venir en `null` (ambos, nunca uno solo); ocultar el dato en vez de mostrar "—" cuando ambos sean `null`.

`POST /wind/devices/<id>/revoke/` → éxito `{"ok": true}` (200); errores `{"ok": false, "code": "not_found"}` (404, mismo resultado si el id no existe o es de otra cuenta, a propósito), `{"ok": false, "code": "already_revoked"}` (409). Un `{"ok": true}` ya confirma el efecto completo -- no hace falta ninguna otra llamada.

## 3. Notificación push de revocación

Cuando un dispositivo se revoca (dashboard, otra sesión propia, o en bloque por cambio de contraseña/cierre de cuenta), su WS recibe `{"type":"device_revoked","reason":"revoked_by_subscriber"|"password_changed"|"account_closed"}` y el backend cierra la conexión. **El cliente debe borrar su `device_token` local y forzar un logout real** -- no solo limpiar el storage y quedarse en pantalla como si nada (ver lección "a" abajo).

`device_list_changed` es distinto: le llega a los **demás** dispositivos conectados de la misma cuenta (no al que se revocó/registró), sin `reason`. Se dispara al revocar uno o registrar uno nuevo -- señal de "refrescá tu `GET /wind/devices/` si la tenés en pantalla", no implica pérdida de acceso.

## Los 5 casos de uso posibles

| # | Quién revoca | Objetivo | Qué recibe quien revoca | Qué recibe/debe hacer el objetivo |
|---|---|---|---|---|
| 1 | Backend/dashboard admin | Otro dispositivo, conectado | `{"ok": true}` (REST) | Push `device_revoked` en vivo -- reaccionar de inmediato |
| 2 | La propia app | Otro dispositivo suyo, conectado | `{"ok": true}` | Igual que 1 |
| 3 | La propia app | Otro dispositivo suyo, desconectado | `{"ok": true}` | Nada en el momento; se aplica en el próximo intento de reconexión (`device_token_invalid`) |
| 4 | El propio dispositivo en uso (autorrevocación) | Sí mismo | `{"ok": true}` **y además** el push casi al mismo tiempo -- tratar como una sola acción | Reaccionar igual que 1/2 |
| 5 | Backend/dashboard admin | Otro dispositivo, desconectado | `{"ok": true}` | Igual que 3 |

Aparte, cambio/recuperación de contraseña y cierre de cuenta revocan **todos** los dispositivos de una vez (`reason="password_changed"`/`"account_closed"`).

## Lecciones de una integración real (aplican a cualquier cliente, no solo appVideo)

**a) `device_revoked` tiene que forzar un logout real, no solo limpiar storage.** Es fácil implementar el WS para que al recibirlo borre el `device_token` y cierre el socket -- pero si ahí se queda, el usuario sigue viendo la app como logueado con una sesión que el backend ya considera terminada. La reacción esperada es la misma que un logout forzado: limpiar toda la sesión local (incluida la de PanAccess/contenido si es independiente) y redirigir a login. Conviene un mecanismo de notificación global (evento/callback al nivel más alto de la app) para que la capa de red le pida a la UI que navegue.

**b) Un logout normal NO debe borrar el `device_token` -- y hay que aplicarlo en un solo lugar central.** Si el logout limpia todo sin distinguir "mismo usuario, va a volver a entrar en este dispositivo" de "otro usuario va a usar este dispositivo", el primer caso pierde su token sin necesidad y el backend crea un `DeviceSession` nuevo cada vez, duplicando el dispositivo en la lista. La lección real (de un bug que se reprodujo varias veces en appVideo hasta centralizarlo): no alcanza con arreglar un solo botón de logout, hay que preservar el token en la función compartida que usan **todos** los caminos de logout/pre-login. Es seguro preservarlo "a ciegas" incluso en un dispositivo compartido entre cuentas -- el backend rechaza con `device_token_invalid` cualquier token que no corresponda, y ahí corresponde reintentar una sola vez sin el token viejo.

**c) Marcar "este dispositivo" en la lista debe ser una lectura reactiva, no puntual.** El registro por WebSocket se dispara en paralelo al login sin que nada lo espere -- puede tardar más que un `GET` a la lista. Si la pantalla de dispositivos lee el `id` propio guardado una sola vez al montarse, puede quedarse con un valor vacío para siempre en esa sesión si se abrió antes de que el registro terminara.

**d) Los DEMÁS dispositivos conectados también necesitan un aviso de que la lista cambió**, no solo el dispositivo afectado -- de ahí `device_list_changed` (arriba).

## Estado en appVideo (web)

Implementado (`deviceAuthService.js`, `deviceSessionService.js`, `loginFlow.js`, `LinkedDevicesPanel.jsx`), con las 4 correcciones de la sección "Lecciones" ya aplicadas. Dos gaps conocidos, sin resolver (no son bugs del backend):

1. `splashAuthFlow.js` -- cuando la sesión de PanAccess sigue válida y la licencia se reactiva sin pasar por `loginAndActivateLicense`, la app nunca reabre `ws/device/` en ese refresh, así que no recibe `device_revoked` en vivo hasta el próximo login real (el `device_token` guardado sigue siendo válido igual).
2. `loginFlow.js` -- el registro del dispositivo se dispara sin esperar (`await`) justo después de marcar la sesión como logueada; si la pestaña se recarga/navega en esa ventana muy angosta, el backend ya creó el `DeviceSession` pero el cliente nunca llegó a persistir el token.

## Qué debe implementar iOS/Android nativo

- Todo el protocolo desde cero: abrir `ws/device/` tras cada login (manual, social, o reactivación de sesión con JWT todavía válido), guardar `device_token`/`id` en Keychain/Keystore, reenviarlo en la próxima conexión.
- La pantalla de "dispositivos vinculados" (`GET /wind/devices/` + botón revocar por `id`), marcando el propio dispositivo con el `id` recibido en `device_registered` -- de forma reactiva (lección "c"), no en una sola lectura al montar.
- El manejo de `device_revoked` como logout forzado real (lección "a") -- crítico, es el bug más importante que se encontró en la integración de referencia.
- Centralizar la preservación de `device_token` en el logout normal en un solo lugar (lección "b"), no repetirlo por pantalla.
- Reaccionar a `device_list_changed` si la pantalla de dispositivos está abierta, refrescando la lista.
- Considerar desde el diseño inicial los dos gaps que quedaron abiertos en appVideo (reapertura del WS tras reactivación de sesión sin login completo, y esperar la confirmación de registro antes de dar el login por "completamente terminado") para no repetir el mismo bug.
