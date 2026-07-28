# Guía única de integración: backend Wind ↔ apps (TV, mobile, web)

Fecha: 2026-07-28.
Reemplaza y consolida: `docs/GUIA_INTEGRACION_APPS.md` (2026-07-22) y `docs/INTEGRACION_PAREO_TV_DISPOSITIVOS.md` (2026-07-27) -- ambos quedan como referencia histórica, pero **esta es la fuente única a partir de ahora**; si algo difiere entre ellos y este documento, vale lo que dice acá.
Referencia de fondo: `docs/AUDITORIA_DECISIONES_Y_PENDIENTES.md` (todas las secciones de Fases 1-4 y revisiones posteriores).

Organización de este documento: primero los conceptos comunes a todas las plataformas (sección 0), después lo que debe implementar **cada plataforma específicamente** (secciones 1 TV, 2 Mobile, 3 Web), después las funciones transversales que usan las tres (sección 4 dispositivos vinculados, sección 5 password/cuenta), y por último tabla de endpoints, manejo de errores, y pendientes (secciones 6-8).

---

## 0. Conceptos comunes a las tres plataformas

- **JWT de sesión**: se obtiene con login manual (`POST /api/auth/login/`) o login social (`POST /wind/auth/google/` / `/wind/auth/facebook/`). Es el mismo JWT que usa el resto de la API autenticada (`/api/v1/profile/...`, `/wind/devices/...`).
- **`subscriber_code` es siempre opaco.** Nunca lo construye, parsea ni valida el cliente -- se guarda tal cual el valor que entrega login/registro y se reenvía sin tocar. El backend nunca lo pide como input libre: siempre lo resuelve del lado del servidor a partir del JWT autenticado. Desde la Fase de prefijos, los suscriptores **nuevos** llevan `BG$<progresivo>` (alta social Google), `BF$<progresivo>` (alta social Facebook), `BM$<documento>` (alta manual con documento) o `BM$AUTO<progresivo>` (alta manual sin documento). Los suscriptores que ya existían conservan su código viejo (documento crudo o `AUTOn`) -- no se migran.
- **`udid` + `temp_token`**: pareja de credenciales de un pareo de TV en curso. `udid` (8 hex) identifica el intento; `temp_token` es el secreto real (sin él, adivinar el `udid` no sirve de nada). Los genera la TV al iniciar el pareo y expiran a los 5 minutos si no se completa.
- **`device_token`**: secreto de 32+ bytes que identifica a un dispositivo (TV, celular o navegador web) como "dispositivo vinculado" de la cuenta, independiente del pareo de TV. Lo entrega el backend la primera vez que ese dispositivo se registra en `/ws/device/`; el cliente debe guardarlo de forma persistente y reenviarlo en la próxima conexión para refrescar el registro en vez de crear uno nuevo.
- **Prerrequisito de JWT para login manual: YA RESUELTO.** Las dos guías anteriores marcaban esto como pendiente ("el login manual no pasa por JWT"). La solución que ambas proponían (llamar también a `POST /api/auth/login/` tras el login manual/social/pareo de TV, solo para obtener el JWT que usa "dispositivos vinculados") **ya está implementada del lado cliente en appVideo** (`deviceAuthService.js` + `deviceSessionService.js`, ver sección 3) y no requiere ningún cambio adicional en el backend. Mobile e iOS/Android deben replicar el mismo patrón: dos logins con las mismas credenciales, uno contra PanAccess (contenido) y otro contra `/api/auth/login/` (JWT, solo para las funciones de este documento).

---

## 1. TV (Smart TV -- webOS/Tizen/Android TV/etc.)

### 1.1 Pareo manual/QR (Fase 1)

1. La TV pide un código: `GET /wind/request-udid-manual/`, con header opcional `X-Device-Public-Key` (base64 de una clave pública RSA en PEM generada por la propia TV para este pareo -- la privada nunca sale de la TV). Rate limit: 1 cada 5 minutos por huella de dispositivo (429 `DEVICE_FP_RATE_LIMIT_EXCEEDED`). Respuesta (201): `udid`, `temp_token`, `expires_at`, `expires_in_minutes` (5), `device_fingerprint`, `remaining_requests`. La TV muestra el `udid` (código corto) y guarda el `temp_token` en memoria.
2. La TV se conecta a `ws/auth/` y manda `{"type":"auth_with_udid","udid":"...","temp_token":"...","app_type":"web","app_version":"1.0"}` (`app_type` uno de `web|lg|samsung|android|androidtv|amazon|iOS|iOStv`). Mensajes posibles: `{"type":"pending",...}` (nadie asoció todavía), `{"type":"ping"}` cada 30s (responder `pong`, o se cierra a los 180s de inactividad), `{"type":"timeout",...}` (300s sin completarse, pedir `udid` nuevo), `{"type":"auth_with_udid:result","status":"ok"|"error",...}`.
3. Con `status:"ok"`, el resultado trae `encrypted_credentials` (`encrypted_data`, `encrypted_key`, `iv`, `algorithm`) -- esquema híbrido AES-256-CBC + RSA-OAEP(SHA-256). Si la TV mandó `X-Device-Public-Key` en el paso 1 (recomendado para todo desarrollo nuevo), desencripta `encrypted_key` con su propia clave privada efímera. Si no la mandó, el backend usó la clave RSA estática legada por `app_type` -- vigente solo para compatibilidad con clientes viejos; no usar en integraciones nuevas.

### 1.2 Pareo vía login social del celular (Fase 2)

Diseño: "solo autorizar la TV" -- el password real de PanAccess nunca llega al celular, solo viaja cifrado del backend a la TV por el mismo WebSocket de 1.1.2. Del lado de la TV **no cambia nada** respecto a 1.1: sigue esperando en `ws/auth/` y recibe el mismo `auth_with_udid:result`. La parte que cambia es del celular (ver sección 2.2).

### 1.3 Dispositivos vinculados en TV (opcional)

Con el prerrequisito de JWT resuelto (sección 0), la TV también puede implementar la sección 4 (dispositivos vinculados) si el producto lo requiere -- mismo protocolo `ws/device/` que mobile/web. No es obligatorio: una TV que solo hace pareo (1.1/1.2) y nunca llama a `/api/auth/login/` directamente queda fuera de "dispositivos vinculados" sin que eso rompa nada.

---

## 2. Mobile (iOS / Android)

### 2.1 Login

- Manual: `POST /api/auth/login/`, body `{"username": "<email/documento/login1 libre>", "password": "..."}` → `{"access","refresh","user":{"pk","email","first_name","last_name","subscriber_code"}}`.
- Social: `POST /wind/auth/google/` o `/wind/auth/facebook/`, body `{"access_token": "<id_token de Google o access token de Facebook>"}` → misma forma más `panaccess_credentials` (`null` o `{"login1","password","login2","subscriberCode"}`).
- **Login manual = dos llamadas con las mismas credenciales** (ver sección 0): una a PanAccess (`clientLogin`, ya existente, sirve contenido/streaming) y otra a `/api/auth/login/` (JWT nuevo, solo para las funciones de este documento). El equipo debe decidir qué hacer si una tiene éxito y la otra falla, y recordar que tras cambiar la contraseña (sección 5.1) hay que repetir **ambas** llamadas, no solo una.
- `SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER` (flag de negocio, hoy `false`): si se activa, un login social con correo sin suscriptor existente responde 400 con `non_field_errors: ["SubscriberNotFound: ..."]` -- detectar por la subcadena `"SubscriberNotFound"`, no por el texto completo.

### 2.2 Iniciar pareo de TV desde el celular

- **Vía login social (flujo principal):** agregar `udid` y `temp_token` (leídos del QR de la TV) al mismo body de `POST /wind/auth/google|facebook/`. Respuesta: `panaccess_credentials` siempre `null`, más `udid_pairing`: `{"ok":true,"udid","subscriber_code"}` o `{"ok":false,"code":"missing_params"|"invalid_udid"|"invalid_temp_token"|"expired"|"not_pending"|"rate_limited"|"subscriber_not_found"|"subscriber_unresolved"|"internal_error","error":"..."}`. Sin `udid`/`temp_token` en el body, el login social funciona exactamente igual que siempre (cambio 100% aditivo).
- **Vía pareo manual (fallback, sin login social):** `POST /wind/validate-and-associate-udid/`, body `{"udid","temp_token","subscriber_code","sn","operator_id","method":"automatic"|"manual"}` (`sn` es una smartcard del suscriptor, se puede traer de `GET /api/v1/profile/products/`). Respuesta 200 con `{"message","udid","subscriber_code","smartcard_sn","status","validated_at"}`. Errores 400 (udid/temp_token/SN inválido, cuenta bloqueada, SN ya asociado a otro UDID) y 429 (1/min por udid).
- En ambos casos, el celular no necesita abrir ningún WebSocket para este flujo -- la TV recibe el resultado por su propio `ws/auth/` (sección 1).

### 2.3 Dispositivos vinculados y 2.4 Password/cuenta

Ver secciones 4 y 5 -- mismo alcance, protocolo y endpoints que el resto de las plataformas.

---

## 3. Web (appVideo)

### 3.1 Estado real: dispositivos vinculados ya implementado del lado cliente

A diferencia de lo que decían las dos guías anteriores ("ninguna app cliente implementó esto todavía"), en una revisión de `D:\appVideo` se confirmó que **el prerrequisito de JWT y el registro de dispositivo ya están programados**, en:

- `src/services/deviceAuthService.js` -- login contra `/api/auth/login/`, refresh contra `/api/auth/token/refresh/`, y `authorizedDeviceRequest()` (fetch autenticado con reintento automático de refresh en 401). Activación opt-in por brand: `login.deviceSession.enabled` en `brands.js` -- si un brand no lo activa, este módulo no hace ninguna llamada de red.
- `src/services/deviceSessionService.js` -- abre `wss://.../ws/device/?token=<access>`, manda `register_device` (con `device_token` guardado si existe), persiste el `device_token` devuelto en `localStorage` namespaced por brand, reintenta una vez con refresh si el WS cierra por JWT vencido, y expone `closeActiveDeviceSession()` para cerrar la conexión en logout.
- `src/services/loginFlow.js` -- llama a lo anterior (`maybeEstablishDeviceSession`, fire-and-forget) desde `loginAndActivateLicense`, cubriendo login manual, social, TV pareada y reactivación de sesión.

Es una implementación correcta en su diseño (persistencia en `localStorage`, reenvío del `device_token` existente para refrescar en vez de duplicar, limpieza en logout para no arrastrar credenciales de un usuario a otro). Se encontraron dos gaps concretos, relevantes para el reporte de dispositivos duplicados al refrescar pantalla:

1. **`src/services/splashAuthFlow.js`, líneas 31-41 (`resolveSplashDestination`):** cuando la sesión de PanAccess sigue siendo válida y la licencia se reactiva con éxito, la función retorna directo a la ruta post-login **sin volver a llamar a `loginAndActivateLicense`/`maybeEstablishDeviceSession`**. Es el camino más común de un refresh de pantalla con sesión activa -- en ese caso la app nunca reabre `ws/device/`, así que deja de recibir `device_revoked` en vivo hasta el próximo login real, aunque el `device_token` guardado sigue siendo válido.
2. **`src/services/loginFlow.js`, línea 247:** `maybeEstablishDeviceSession(...)` se llama sin `await`, inmediatamente después de `userSession.setLoggedIn(...)`. El backend crea el `DeviceSession` en el momento en que recibe `register_device` (antes de mandar la confirmación `device_registered`); el cliente solo guarda el `device_token` al recibir esa confirmación. Si la pestaña se recarga o navega en la ventana entre esos dos momentos (login recién hecho, registro todavía en vuelo por la red), el `DeviceSession` ya quedó creado en el backend pero el cliente nunca llegó a persistir su token -- el siguiente intento de registro no tiene `existingToken` para reenviar y crea uno nuevo. Esto explica de forma más precisa el síntoma reportado ("vinculo un dispositivo, refresco pantalla, queda como nuevo"): no es que el `device_token` se pierda por no guardarse (sí se guarda, en `localStorage`), sino que hay una ventana real, aunque angosta, en la que el backend ya creó el registro pero el cliente todavía no se enteró.

Ninguno de los dos es un bug del backend -- el backend hace exactamente lo que debe (crear si no le llega `device_token`, refrescar si le llega uno válido). Son ajustes de lado cliente: (1) decidir si conviene reabrir `ws/device/` también en el camino rápido del splash, y (2) considerar si esperar (`await`) la confirmación antes de dejar avanzar la navegación, o algún mecanismo de recuperación si se interrumpe a mitad de camino. No implementados en esta pasada -- quedan documentados para cuando se decida tocar `appVideo`.

### 3.2 Resto del alcance

Mismo que mobile para password/cuenta (sección 5). Web normalmente no inicia pareos de TV, pero podría usar la misma sección 2.2 si el negocio lo requiere.

---

## 4. Dispositivos vinculados (protocolo común a TV, mobile y web)

### 4.1 Registro por WebSocket

`ws/device/?token=<jwt de access>`. El JWT va como query param (no hay forma de mandar headers en el handshake WS desde algunas plataformas). Cierres: **4001** (JWT inválido/expirado, o límite de conexiones excedido -- viene con motivo y segundos de espera), **4004** (JWT válido pero no se pudo resolver `subscriber_code`).

Mensaje para registrarse: `{"type":"register_device","device_type":"...","device_model":"...","device_token":"<opcional, el guardado la vez anterior>"}`. `device_type` recomendado: `iOS`, `android`, `web`, `lg`, `samsung` (cualquier otro string se acepta igual, truncado a 50 caracteres, pero conviene usar estos para que el dashboard filtre bien).

Respuesta: `{"type":"device_registered","device_token":"<token>","is_new":true|false}`. Guardar `device_token` de forma persistente y segura (Keychain/Keystore en mobile, `localStorage` en web) y reenviarlo en la próxima conexión para refrescar en vez de crear un registro nuevo. Límite: 20 dispositivos **nuevos** por hora por suscriptor (refrescar uno existente no cuenta). Si se excede: `{"type":"error","code":"rate_limited",...}` y cierre 1011. Si el `device_token` enviado no es válido (revocado o de otra cuenta): `{"type":"error","code":"device_token_invalid",...}` y cierre 1011 -- tratar igual que una revocación (borrar el token local, registrarse de nuevo sin él).

`ping`/`pong`: el servidor manda `{"type":"ping"}` cada 30s, el cliente responde `{"type":"pong"}`.

**Nota abierta:** `device_registered` hoy no devuelve el `id` interno del `DeviceSession` (solo `device_token`/`is_new`), así que ningún cliente puede marcar "este dispositivo" en la lista de 4.2 sin heurísticas. Sigue pendiente de decisión (ver sección 8).

### 4.2 Listar y revocar (REST, JWT)

`GET /wind/devices/` → `{"devices": [{"id","device_type","device_model","first_seen_at","last_seen_at","client_ip"}, ...]}` (el `device_token` nunca se expone acá).

`POST /wind/devices/<id>/revoke/` → éxito `{"ok": true}` (200); errores `{"ok": false, "code": "not_found"}` (404, mismo resultado si el id no existe o es de otra cuenta -- a propósito, para no filtrar información), `{"ok": false, "code": "already_revoked"}` (409), `{"ok": false, "code": "subscriber_unresolved"}` (400). Un `{"ok": true}` con 200 es la confirmación completa: significa que el `DeviceSession` quedó en `status="revoked"` en la base y que se programó el aviso en vivo por WebSocket -- no hace falta ninguna otra llamada para confirmar el efecto, aunque si se quiere verificar explícitamente, un `GET /wind/devices/` posterior ya no debe listar ese `id`. Límite: 60 solicitudes/minuto.

### 4.3 Notificación push de revocación

Cuando un dispositivo se revoca (dashboard, o en bloque por cambio de contraseña/cierre de cuenta, sección 5), su WS recibe `{"type":"device_revoked","reason":"revoked_by_subscriber"|"password_changed"|"account_closed"}` y el backend cierra la conexión. El cliente debe borrar su `device_token` local y forzar login de nuevo. Si el dispositivo no está conectado en ese momento, el efecto se aplica igual la próxima vez que intente reconectarse o refrescar su registro (el `device_token` ya no sirve).

---

## 5. Password y cuenta (común a las tres plataformas)

### 5.1 Cambiar contraseña

`POST /api/v1/profile/password/` (JWT), body `{"code": "<subscriber_code>", "newPass": "..."}` → `{"success": true, "message": "Contraseña actualizada"}` (o 400/502).

**Efecto colateral automático:** invalida todos los JWT ya emitidos y revoca **todos** los `DeviceSession` de la cuenta -- incluida la propia sesión que hizo el cambio. Tras un cambio exitoso, la app debe volver a loguearse (JWT nuevo) y volver a registrar el dispositivo (sección 4.1).

### 5.2 Contraseña olvidada

`POST /api/auth/password/forgot/` (`email`, `recaptcha_token`) → siempre 200 genérico. `POST /api/auth/password/reset-confirm/` (`token`, `newPass`, `confirmPass`) → 200 si aplicó, 400 si el token es inválido/expirado/usado. Mismo efecto colateral que 5.1.

### 5.3 Cerrar cuenta

`POST /api/v1/profile/account/close/` (JWT + reCAPTCHA), body `{"code","confirm" (=code),"reason","dry_run"}`. `dry_run:true` no borra nada, solo devuelve un plan. Cierre exitoso: `{"success":true,"subscriber_code","panaccess":{...},"local":{...,"device_sessions_revoked":N,"udid_revoked":N},"closure_log_id","re_registration":"allowed_without_trial",...}`. Cierre parcial (PanAccess falló pero el acceso local ya se cortó): `{"success": false, ...}` -- el corte de acceso local (JWT, dispositivos, pareos) ocurre siempre, incluso si PanAccess no terminó.

---

## 6. Tabla resumen de endpoints

| Acción | Método y ruta | Auth |
|---|---|---|
| Login manual | `POST /api/auth/login/` | -- |
| Login social (Google) | `POST /wind/auth/google/` | -- |
| Login social (Facebook) | `POST /wind/auth/facebook/` | -- |
| Refrescar JWT | `POST /api/auth/token/refresh/` | refresh token |
| Registro manual público | `POST /wind/create-subscriber/` | -- |
| Generar UDID (TV) | `GET /wind/request-udid-manual/` | -- |
| Pareo manual TV | `POST /wind/validate-and-associate-udid/` | -- |
| Estado de un pareo (polling) | `GET /wind/validate/?udid=...&temp_token=...` | -- |
| Desvincular TV de un UDID | `POST /wind/disassociate-udid/` | -- |
| Registrar/refrescar dispositivo | WS `wss://.../ws/device/?token=<jwt>` | JWT (query) |
| Listar mis dispositivos | `GET /wind/devices/` | JWT |
| Revocar un dispositivo | `POST /wind/devices/<id>/revoke/` | JWT |
| Cambiar contraseña | `POST /api/v1/profile/password/` | JWT |
| Olvidé mi contraseña | `POST /api/auth/password/forgot/` | -- |
| Confirmar reset de contraseña | `POST /api/auth/password/reset-confirm/` | -- |
| Eliminar/cerrar cuenta | `POST /api/v1/profile/account/close/` | JWT |
| Mi perfil / suscriptor | `GET /api/v1/profile/me/` | JWT |
| Mis productos/smartcards | `GET /api/v1/profile/products/` | JWT |

---

## 7. Manejo de errores y rate limiting (aplica a todo lo anterior)

- Los endpoints de pareo devuelven 429 con `retry_after` (segundos) -- respetar ese tiempo, no reintentar en loop inmediato.
- Ningún endpoint documentado devuelve el detalle crudo de una excepción interna (`str(e)`) -- los mensajes de error son genéricos por diseño; reportar el `code`/`error_type` recibido, no parsear texto libre.
- Todos los `temp_token` se comparan en tiempo constante en el backend -- transportarlos tal cual, sin recortar ni cambiar mayúsculas/minúsculas.

---

## 8. Pendientes / decisiones abiertas (estado a 2026-07-28)

- **`crypto_tv.py` sin AEAD/HMAC** -- el cifrado híbrido (AES-256-CBC + RSA-OAEP) da confidencialidad pero no integridad/autenticidad del ciphertext. Pendiente de decisión sobre migrar a un esquema autenticado (AES-GCM u HMAC-then-encrypt).
- **Rate limiting de UDID/WebSocket no atómico** (`websocket_utils.py`): varias funciones (`check_udid_rate_limit`, `check_temp_token_rate_limit`, `check_device_fingerprint_rate_limit`, y el par `check_websocket_rate_limit`/`increment_websocket_connection`) separan la lectura del contador de su incremento en dos operaciones de caché distintas -- bajo concurrencia alta, dos requests simultáneas pueden leer el mismo valor antes de que ninguna incremente, dejando pasar más tráfico del límite configurado. `check_websocket_limits` (usada por `ws/device/`) y `check_token_bucket_lua` (usada por el límite de 20 dispositivos/hora) sí son o casi son atómicas (la primera usa `INCR` de Redis; la segunda es un contador de caché simple sin operación atómica real, a pesar de su nombre/comentario que menciona Lua). Pendiente de decisión.
- **`check_websocket_limits` fail-open + fingerprint evadible**: si Redis falla, la función deja pasar la conexión (`return True, None, 0`) en vez de rechazarla -- por diseño para no tumbar el servicio ante un Redis caído, pero es una superficie de evasión real (tumbar/saturar Redis externamente para saltarse el límite). El fingerprint, aunque ya no lo puede declarar el cliente (se deriva server-side de headers), sigue siendo evadible rotando esos mismos headers entre requests.
- **`UDIDAuthRequest.attempts_count`**: no es código muerto (se usa en `is_valid()` como gate de `max_attempts=5`) -- el hallazgo real es que se incrementa en cada *consulta de estado* (`GET /wind/validate/`, usado para polling mientras se espera el pareo), no en cada intento real de asociación. Una TV/app que haga polling normal cada pocos segundos puede agotar el cupo de 5 solo esperando, sin que nadie haya intentado nada indebido. Pendiente decidir si el contador debe incrementarse solo en intentos reales de asociación (`POST /wind/validate-and-associate-udid/`), no en cada poll de estado.
- **Orden lexicográfico en `subscriber_code_generator.py`**: `ListOfSubscriber.objects.filter(code__startswith=prefix).order_by('-code').first()` ordena como texto, no como número -- por ejemplo, entre "AUTO9" y "AUTO10", el orden de texto pone "AUTO9" primero (compara carácter por carácter: '9' > '1'), así que una vez que existen códigos de dos cifras, la próxima generación puede volver a partir de un número más bajo del que realmente corresponde, desperdiciando intentos contra códigos ya usados. Alcance ya angosto (solo aplica al prefijo `AUTO` histórico, no a los nuevos `BG$`/`BF$`/`BM$`), pero sigue sin corregirse.
- **`client_ip` en `device_consumers.py`/`consumers.py` siempre resuelve al peer inmediato** (`self.scope.get("client")`), sin pasar por ningún filtro de proxy confiable -- a diferencia de `get_client_ip()` (HTTP, en `websocket_utils.py`) y `sync_admin_ip_middleware.py`, que ya validan `REMOTE_ADDR` contra `TrustedProxyConfig.TRUSTED_PROXIES` antes de confiar en `X-Forwarded-For`. Si el despliegue real corre detrás de un proxy/load balancer, esto guarda la IP del proxy, no la del cliente, en los WS. Solo afecta logs/auditoría, no control de acceso. Pendiente aplicar el mismo patrón de proxy confiable que ya existe para HTTP.
- **`device_registered` no devuelve el `id` interno del `DeviceSession`** -- ningún cliente puede marcar "este dispositivo" en la lista de la sección 4.2 sin heurísticas. Ajuste de bajo riesgo (agregar un campo al mensaje ya existente), pendiente de decisión sobre cuándo tocar `device_consumers.py`.
- **Password en texto plano** en el correo de bienvenida y en la respuesta de login social -- decisión de negocio ya aceptada, se re-lista solo porque sigue siendo una superficie de exposición real.
- **Site-key de reCAPTCHA para mobile**: todavía sin definir cuál usar en "olvidé mi contraseña" / "eliminar cuenta" desde iOS/Android.
- **Brand `bromteck` en `appVideo`** sigue apuntando a `http://` en vez de `https://` -- error menor, pendiente de que el equipo de `appVideo` lo resuelva.
- **Posible incidente de pérdida de datos** (`docs/limpiar tablas.txt` + `restaurar_tablas.py`): cerrado -- las bases de datos se restauraron después de varias migraciones, no requiere auditoría adicional.
- **Formato del QR/código de la TV**: sigue sin estandarizar en ningún documento del backend -- lo define el equipo de `appVideo`; confirmar antes de programar el parseo en mobile.
