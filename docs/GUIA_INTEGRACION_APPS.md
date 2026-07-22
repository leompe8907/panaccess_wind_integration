# Guía de integración backend ↔ apps (TV, mobile, web)

Fecha: 2026-07-22
Referencia: `docs/AUDITORIA_DECISIONES_Y_PENDIENTES.md` (secciones 27-33, donde se implementó todo lo que describe esta guía)

## Alcance de este documento

Esta guía describe, funcionalidad por funcionalidad, **qué debe implementar cada app cliente (TV, mobile, web) y cómo**, para consumir todo lo que ya está construido y verificado del lado del backend (Fases 1 a 4 más la segunda auditoría). Es un documento de referencia técnica para la pasada de integración final -- **ninguna de estas conexiones está implementada todavía del lado de los clientes** (esa integración está deliberadamente diferida hasta que el backend estuviera terminado). Todos los endpoints, formatos y flujos aquí descritos ya están implementados, probados (`py_compile` + `manage.py check`) y documentados en el backend.

Convenciones usadas en este documento: los campos de request/response se listan con su nombre literal tal como los espera/devuelve el backend. Los códigos de error (`code`) son valores exactos que el backend devuelve -- se recomienda que el cliente compare contra ellos en vez de contra el texto del mensaje, salvo donde se indique lo contrario.

---

## 1. Autenticación y registro

### 1.1 Login manual

`POST /api/auth/login/`

Body: `{"username": "<texto libre>", "password": "<password>"}`.

El campo `username` es deliberadamente libre: el backend intenta, en orden, email/username de Django, luego `login1`/`login2`/email de PanAccess, y si es numérico, un descubrimiento por `uniqueLogin`. En la práctica el cliente puede mandar ahí el email, el documento/cédula, o el login1 que PanAccess le haya dado -- todos funcionan.

Respuesta (200): `{"access": "<jwt>", "refresh": "<jwt>", "user": {"pk", "email", "first_name", "last_name", "subscriber_code"}}`.

**Importante para el cliente:** `subscriber_code` viene en la respuesta de login -- guardarlo tal cual (ver 1.4, es un string opaco) para usarlo en cambio de contraseña / cierre de cuenta (sección 4).

Cookies: por defecto (`JWT_USE_COOKIES=false` en este despliegue) los tokens solo van en el body. Si en algún momento se activa esa variable, además se setean cookies `wind-auth`/`wind-refresh-token`, pero el body sigue trayendo los tokens igual -- no reemplaza el body, lo complementa.

### 1.2 Login social (Google / Facebook)

`POST /wind/auth/google/` y `POST /wind/auth/facebook/`.

Body (ambos): `{"access_token": "<token>"}`. Para Google, ese valor es el id_token JWT que entrega Google Identity Services (el backend lo trata como tal, no como OAuth access_token clásico).

Respuesta estándar: `{"access", "refresh", "user": {...}}` (igual que login manual) más `panaccess_credentials`, que puede ser:
- `null` -- no se pudieron resolver credenciales (raro; queda logueado del lado de Django pero sin PanAccess).
- `{"login1", "password", "login2", "subscriberCode"}` -- credenciales reales de PanAccess, para que el cliente pueda iniciar sesión directa en PanAccess si lo necesita.

**Caso especial -- pareo de TV vía login social (ver sección 3):** si el body además incluye `udid` y `temp_token`, `panaccess_credentials` siempre viene `null` (a propósito -- el password real nunca debe tocar el celular en ese flujo) y aparece un campo extra `udid_pairing` con el resultado del pareo.

### 1.3 Bandera `SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER`

Activable en `.env` del backend (`false` por defecto, sin cambio de comportamiento). Cuando está en `true`, un login social con un correo que NO tiene todavía un suscriptor en PanAccess **ya no se auto-registra** (antes sí, con prueba gratis) -- el backend responde HTTP 400 con:

```json
{"non_field_errors": ["SubscriberNotFound: No existe un suscriptor asociado a este correo."]}
```

El cliente debe detectar este caso específico buscando la subcadena `"SubscriberNotFound"` dentro de `non_field_errors[0]` (no comparar el mensaje completo, por si cambia de redacción) y mostrar un aviso apropiado ("no tienes una suscripción todavía") en vez de tratarlo como un error genérico de login.

Esta misma bandera también afecta el pareo de TV vía login social (sección 3.2): si el correo no tiene suscriptor, `udid_pairing.code` viene como `"subscriber_not_found"` en vez de intentar crear uno.

### 1.4 Formato de `subscriber_code` (tratar siempre como opaco)

Los suscriptores **nuevos** ahora reciben un `subscriber_code` con prefijo según cómo se registraron:

- `BG$<progresivo>` -- alta por login social Google.
- `BF$<progresivo>` -- alta por login social Facebook.
- `BM$<documento>` -- alta manual, cuando el usuario da su documento/cédula.
- `BM$AUTO<progresivo>` -- alta manual sin documento (caso raro, el formulario lo permite).

Los suscriptores que ya existían en producción **no se tocan** -- siguen con su código viejo (documento crudo sin prefijo, o `AUTOn`).

**Regla para todos los clientes:** el `subscriber_code` debe tratarse siempre como un string opaco. Nunca construirlo, parsearlo ni asumirle un formato -- el backend nunca lo pide como input del usuario (login, pareo, revocar dispositivo, cambiar password, cerrar cuenta: todo resuelve el código del lado del servidor a partir del JWT autenticado o de otros identificadores, jamás de un parámetro armado por el cliente). Simplemente guardar y reenviar tal cual el valor que el backend entregó en login/registro.

### 1.5 Registro manual (público, sin login previo)

`POST /wind/create-subscriber/` (requiere `recaptcha_token` en el body; gateado por `FeatureConfig.CREATE_SUBSCRIBER_PUBLIC_ENABLED`).

Body: `lastName`, `firstName`, `email` (requeridos); `code` o `document_number` (documento/cédula, opcional), `document_type`, `phone`, `hcId`, `comment`, `countryCode` (default `DO`), `regionId`, `technicalNotes`, `caf` (todos opcionales).

Respuesta síncrona (modo por defecto): `success`, `message`, `subscriber_code`, `alternative_login` (=email), `data{...}`, `contacts_added`/`contacts_errors`, `license_block_added`, `token`, `credentials_url`, `assigned_smartcards`, `product_add_result`.

Respuesta en modo async (`CREATE_SUBSCRIBER_ASYNC_ENRICHMENT=true`, apagado por defecto -- **coordinar con backend antes de asumir este modo**): solo `{"success": true, "message", "subscriber_code", "alternative_login", "provisioning": "async"}`, HTTP 201. En este modo NO vienen `token`/`credentials_url`/`license_block_added`/`contacts_added`/`assigned_smartcards` -- el resto del aprovisionamiento sigue en background.

---

## 2. Pareo de TV -- flujo manual/QR (Fase 1)

Flujo pensado para: la TV muestra un código, el usuario lo asocia desde el celular (o un operador lo asocia manualmente).

### 2.1 La TV pide un código de pareo

`GET /wind/request-udid-manual/`

Header opcional `X-Device-Public-Key`: base64 de una clave pública RSA en PEM, generada por la propia TV para ESTE pareo (clave efímera -- la privada nunca sale de la TV). Si la TV la manda, las credenciales que reciba más adelante vendrán cifradas específicamente para esa clave (ver 2.4); si no la manda, el backend usa el esquema legado de clave estática por `app_type`.

Rate limit: 1 cada 5 minutos por huella de dispositivo (429 `DEVICE_FP_RATE_LIMIT_EXCEEDED` si se excede).

Respuesta (201): `udid` (8 hex), `temp_token` (el secreto real del pareo -- la TV lo necesita para autenticar por WS, ver 2.3), `expires_at`, `expires_in_minutes` (5), `device_fingerprint`, `remaining_requests`.

La TV debe mostrar el `udid` en pantalla (código corto legible) y guardar el `temp_token` en memoria para el paso 2.3.

### 2.2 El celular asocia el código

`POST /wind/validate-and-associate-udid/`

Body: `udid`, `temp_token`, `subscriber_code`, `sn` (serial de la smartcard -- lo elige la app, no el backend), `operator_id`, `method` (`automatic`|`manual`).

Respuesta (200): `message`, `udid`, `subscriber_code`, `smartcard_sn`, `status`, `validated_at`. Este paso es lo que dispara el push hacia la TV (ver 2.3).

### 2.3 La TV espera el resultado por WebSocket

`ws/auth/`

La TV se conecta y envía: `{"type":"auth_with_udid","udid":"...","temp_token":"...","app_type":"web","app_version":"1.0"}` (`app_type` debe ser uno de: `web|lg|samsung|android|androidtv|amazon|iOS|iOStv`).

Mensajes que puede recibir:
- `{"type":"pending","status":...,"detail":...,"timeout":<segundos>}` -- todavía nadie asoció el código; la TV debe seguir esperando.
- `{"type":"ping"}` cada 30s -- la TV debe responder `{"type":"pong"}` (si no, se cierra por inactividad a los 180s).
- `{"type":"timeout","detail":"..."}` -- se agotó el tiempo (300s por defecto); la TV debe pedir un `udid` nuevo (volver a 2.1).
- `{"type":"auth_with_udid:result","status":"ok","result":{"encrypted_credentials":{...}, "security_info":{...}, "expires_at":...}}` -- éxito, credenciales cifradas listas para desencriptar (ver 2.4).
- `{"type":"auth_with_udid:result","status":"error", ...}` -- fallo definitivo (código inválido, expirado, etc.), la TV debe pedir un `udid` nuevo.

### 2.4 Desencriptar las credenciales en la TV

Esquema híbrido AES-256-CBC + RSA-OAEP(SHA-256). El payload trae `encrypted_data`, `encrypted_key`, `iv`, `algorithm`.

- Si la TV mandó `X-Device-Public-Key` en el paso 2.1 (recomendado, esquema nuevo por pareo): la TV desencripta `encrypted_key` con SU PROPIA clave privada (la que generó ella misma y nunca compartió), obtiene la clave AES, y con ella desencripta `encrypted_data`.
- Si no mandó esa clave (esquema legado, clientes viejos): el backend usa una clave RSA estática por `app_type`, ya embebida en el build de esa app -- la TV desencripta con esa clave privada legada, tal como ya lo hace hoy.

**Recomendación:** todo desarrollo nuevo de TV debe usar el esquema de clave efímera por pareo (`X-Device-Public-Key`), no el legado -- es la mejora de seguridad ya señalada en la auditoría original (clave RSA estática expuesta en el bundle).

---

## 3. Pareo de TV vía login social (Fase 2)

Diseño confirmado con el cliente: **"solo autorizar la TV"** -- el celular hace login social normal, pero el password real de PanAccess nunca llega al celular, solo viaja cifrado del backend a la TV (mismo mecanismo de la sección 2).

### 3.1 Qué debe mandar el celular

Un POST normal a `/wind/auth/google/` o `/wind/auth/facebook/` (sección 1.2), agregando `udid` y `temp_token` (los mismos que la TV mostró/generó en el paso 2.1 de la sección anterior).

### 3.2 Qué recibe el celular

`panaccess_credentials` siempre `null`. Además, un campo `udid_pairing` con uno de estos valores:

- `{"ok": true, "udid": "...", "subscriber_code": "..."}` -- éxito, la TV ya está siendo notificada por el WS de la sección 2.3.
- `{"ok": false, "code": "subscriber_not_found", "error": "No existe un suscriptor asociado a este correo."}` -- solo si `SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER` está activo y el correo no tiene suscriptor.
- `{"ok": false, "code": "subscriber_unresolved", "error": "..."}` -- no se pudo resolver/crear el suscriptor (falla interna).
- `{"ok": false, "code": "missing_params"|"invalid_udid"|"invalid_temp_token"|"expired"|"not_pending"|"rate_limited"|"internal_error", "error": "..."}` -- el código de pareo en sí es inválido/expiró/etc.

La TV recibe el resultado exactamente igual que en la sección 2.3 (mismo WS, mismo evento `auth_with_udid:result`) -- no hace falta ningún cambio adicional del lado de la TV entre este flujo y el de la sección 2.

---

## 4. Dispositivos vinculados (Fase 3)

Idea: cualquier app (TV o mobile), al iniciar sesión o arrancar, se identifica ante el backend por WebSocket; el backend le da un `device_token` que la app debe guardar. El usuario ve la lista de dispositivos vinculados en su dashboard (como WhatsApp) y puede desvincular cualquiera; al hacerlo, el backend notifica a ese dispositivo para que borre su token y cierre sesión localmente.

### ⚠️ Prerrequisito no resuelto todavía -- leer antes de implementar

El registro de dispositivo requiere un JWT válido (`?token=` en el WS). Hoy, el login manual de las apps (`clientLogin`, contra PanAccess directo) **no pasa por el sistema JWT de Django en absoluto** -- ningún flujo de la app persiste ni usa un JWT tras un login manual. Esto significa que, tal como está hoy, esta función de dispositivos vinculados **solo funcionaría de forma natural para quien entre por login social** (que sí emite JWT, sección 1.2/1.1).

Antes de integrar esta sección en cualquier cliente que soporte login manual, hay que resolver esto: la opción propuesta (pendiente de confirmar con el cliente) es que el login manual y la finalización del pareo de TV también llamen a `/api/auth/login/` para obtener un JWT exclusivamente para el registro de dispositivo, aunque el resto de la sesión de la app siga funcionando contra PanAccess como hoy. **No implementar esta sección en mobile/web hasta resolver este punto.**

### 4.1 Registro por WebSocket

`ws/device/?token=<jwt de access>`

Si el JWT es inválido/expirado: cierre inmediato, código **4001**. Si es válido pero no se puede resolver el `subscriber_code` del usuario: cierre código **4004**. Si se exceden límites de conexión: cierre **4001** con motivo y segundos de espera.

Mensaje que la app debe enviar para registrarse: `{"type":"register_device","device_type":"...","device_model":"...","device_token":"<opcional, para refrescar un registro existente>"}`.

Respuesta: `{"type":"device_registered","device_token":"<token>","is_new":true|false}` -- la app **debe guardar `device_token`** (no se vuelve a mostrar en ningún otro lado) y reenviarlo en reconexiones futuras dentro del mismo mensaje `register_device` para refrescar en vez de crear un registro nuevo.

`ping`/`pong`: el servidor manda `{"type":"ping"}` cada 30s, la app responde `{"type":"pong"}`.

Límite: máx. 20 dispositivos NUEVOS por hora por suscriptor (los refrescos de un `device_token` ya existente no cuentan contra este límite). Si se excede: `{"type":"error","code":"rate_limited","detail":"..."}` y cierre 1011.

`device_token` no reconocido (refresco con un token inválido): `{"type":"error","code":"device_token_invalid","detail":"..."}` y cierre 1011 -- la app debe tratar esto igual que una revocación (borrar el token local y volver a registrarse desde cero).

### 4.2 Listar y revocar dispositivos (REST, requiere JWT)

`GET /wind/devices/` -- respuesta: `{"devices": [{"id","device_type","device_model","first_seen_at","last_seen_at","client_ip"}, ...]}`. El `device_token` nunca se expone acá -- el dashboard revoca por `id`, no por token.

`POST /wind/devices/<id>/revoke/` -- éxito: `{"ok": true}`. Errores: `{"ok": false, "code": "not_found"}` (404), `{"ok": false, "code": "already_revoked"}` (409), `{"ok": false, "code": "subscriber_unresolved"}` (400).

### 4.3 Notificación push de revocación

Cuando un dispositivo se revoca (desde el dashboard, o automáticamente por cambio de contraseña/cierre de cuenta, ver sección 5), el WS de ese dispositivo recibe `{"type":"device_revoked","reason":"revoked_by_subscriber"|"password_changed"|"account_closed"}` y el backend cierra la conexión. La app debe, al recibir esto: borrar su `device_token` guardado localmente y cerrar la sesión del usuario en ese dispositivo (forzar login de nuevo).

---

## 5. Cambio de contraseña y cierre de cuenta (Fase 4)

### 5.1 Cambiar contraseña

`POST /api/v1/profile/password/` (JWT requerido, más verificación de que `code` pertenece al usuario autenticado).

Body: `{"code": "<subscriber_code>", "newPass": "<nueva contraseña>"}`.

Respuesta: `{"success": true, "message": "Contraseña actualizada"}` o error (400 validación, 502 si PanAccess falla).

**Importante:** al cambiar la contraseña, el backend revoca **todos** los dispositivos vinculados del suscriptor (sección 4.3, `reason="password_changed"`) e invalida todos los JWT emitidos antes del cambio -- sin excepción para "el dispositivo que hizo el cambio". Si la propia app que cambió la contraseña quiere seguir con sesión activa, debe volver a autenticarse (login) después de cambiarla, y volver a registrarse como dispositivo (sección 4.1) si aplica.

### 5.2 Cerrar cuenta

`POST /api/v1/profile/account/close/` (JWT + `IsOwnerSubscriber` + reCAPTCHA).

Body: `{"code": "<subscriber_code>", "confirm": "<debe ser igual a code>", "reason": "<opcional>", "dry_run": true|false}`.

Con `dry_run=true`: no borra nada, solo devuelve un plan (`{"success": true, "dry_run": true, ..., "local_plan": {...}}}`) -- útil para mostrarle al usuario qué va a pasar antes de confirmar.

Cierre real exitoso: `{"success": true, "subscriber_code", "panaccess": {...}, "local": {..., "device_sessions_revoked": N, "udid_revoked": N}, "closure_log_id", "re_registration": "allowed_without_trial", "message": "Cuenta cerrada correctamente."}`.

Cierre parcial (PanAccess falló pero el acceso local ya se cortó): `{"success": false, ..., "message": "Cierre parcial en PanAccess; reintente o revise logs."}` -- el acceso local (JWT, dispositivos, pareos UDID) ya queda cortado en ambos casos, incluso si PanAccess no terminó de procesar el cierre.

---

## 6. Resumen por tipo de cliente

**TV:** implementa secciones 2 (pareo manual/QR) y 3 (pareo vía login social del celular vinculado) -- no necesita login propio, solo el WS de `ws/auth/` y el desencriptado híbrido. Puede además implementar la sección 4 (dispositivo vinculado) usando el JWT que ya recibe indirectamente si se resuelve el prerrequisito de esa sección; si no se resuelve, puede quedar fuera del alcance de "dispositivos vinculados" por ahora.

**Mobile:** implementa login (manual y/o social, sección 1), inicia pareo de TV desde el celular (secciones 2.2 y/o 3.1), y -- una vez resuelto el prerrequisito de JWT en login manual -- dispositivos vinculados (sección 4) y gestión de cuenta (sección 5).

**Web:** mismo alcance que mobile (login, gestión de cuenta), normalmente no inicia pareos de TV pero podría hacerlo con la misma sección 2.2/3.1 si el negocio lo requiere.

---

## 7. Pendientes / decisiones abiertas antes de integrar

- **Prerrequisito de JWT para login manual** (sección 4): sin resolver -- necesita decisión y confirmación explícita antes de integrar dispositivos vinculados para usuarios que entran por login manual.
- **Clave RSA estática en el cliente TV/mobile** (esquema legado de la sección 2.4): sigue vigente en producción, es la vulnerabilidad Crítica original de la auditoría; se recomienda que toda integración nueva use el esquema de clave efímera (`X-Device-Public-Key`) y se abandone el legado apenas sea viable.
- **`SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER`** (sección 1.3): hoy está en `false` (comportamiento actual sin cambios); el cliente de negocio debe confirmar cuándo activarla y coordinar con los equipos de app el manejo del error `SubscriberNotFound` antes de prenderla en producción.
- **Modo async de registro** (`CREATE_SUBSCRIBER_ASYNC_ENRICHMENT`, sección 1.5): apagado por defecto; si se activa, el frontend deja de recibir varios campos de forma síncrona -- coordinar explícitamente antes de asumir ese modo en cualquier cliente.
