# Guía única de integración: backend Wind ↔ apps (TV, mobile, web)

Fecha: 2026-07-29 (actualizado con más lecciones de la misma integración real end-to-end en appVideo -- ver secciones 3.1, 4.2, 4.3 y 4.4).
Actualización 2026-08-31: agregada sección 6, preferencias sincronizadas (control parental + favoritos) -- nueva funcionalidad, mismo JWT de sesión que el resto del documento, sin dependencia de dispositivos vinculados. Detalle de implementación backend/appVideo-web en `docs/SINCRONIZACION_PREFERENCIAS_2026-08-31.md`.
Actualización 2026-09-01: agregada sección 7, logs de diagnóstico para desarrolladores -- endpoint de ingesta sin JWT obligatorio (con API key propia), pensado especialmente para que iOS/Android lo implementen desde el día uno. Detalle de implementación backend en `docs/LOGS_DIAGNOSTICO_2026-09-01.md`.
Actualización 2026-09-02: aclarado en 2.2 el comportamiento de UX esperado del lado de la app (qué pantalla/flujo debe existir, y por qué "hacer login social" ahí NO significa mostrarle al usuario una pantalla de login si ya está logueado) -- esto no estaba especificado antes, solo el contrato de API. Agregada sección 2.5, alternativa de auto-servicio sin escanear QR (dashboard web, no requiere ningún trabajo de las apps) -- ver `docs/PAREO_UDID_AUTOSERVICIO_CUENTA_2026-09-02.md`. Agregada sección 1.1.1, documentando por primera vez el formato exacto del código/QR que muestra la TV (hallazgo #34) -- y una propuesta de formato alternativo (QR como URL versionada) en `docs/PROPUESTA_FORMATO_QR_UDID_2026-09-02.md`, pendiente de decisión.
Reemplaza y consolida: `docs/GUIA_INTEGRACION_APPS.md` (2026-07-22) y `docs/INTEGRACION_PAREO_TV_DISPOSITIVOS.md` (2026-07-27) -- ambos quedan como referencia histórica, pero **esta es la fuente única a partir de ahora**; si algo difiere entre ellos y este documento, vale lo que dice acá.
Referencia de fondo: `docs/AUDITORIA_DECISIONES_Y_PENDIENTES.md` (todas las secciones de Fases 1-4 y revisiones posteriores).

Organización de este documento: primero los conceptos comunes a todas las plataformas (sección 0), después lo que debe implementar **cada plataforma específicamente** (secciones 1 TV, 2 Mobile, 3 Web), después las funciones transversales que usan las tres (sección 4 dispositivos vinculados, sección 5 password/cuenta, sección 6 preferencias sincronizadas, sección 7 logs de diagnóstico), y por último tabla de endpoints, manejo de errores, y pendientes (secciones 8-10).

---

## 0. Conceptos comunes a las tres plataformas

- **JWT de sesión**: se obtiene con login manual (`POST /api/auth/login/`) o login social (`POST /wind/auth/google/` / `/wind/auth/facebook/`). Es el mismo JWT que usa el resto de la API autenticada (`/api/v1/profile/...`, `/wind/devices/...`).
- **`subscriber_code` es siempre opaco.** Nunca lo construye, parsea ni valida el cliente -- se guarda tal cual el valor que entrega login/registro y se reenvía sin tocar. El backend nunca lo pide como input libre: siempre lo resuelve del lado del servidor a partir del JWT autenticado. Desde la Fase de prefijos, los suscriptores **nuevos** llevan `BG$<progresivo>` (alta social Google), `BF$<progresivo>` (alta social Facebook), `BM$<documento>` (alta manual con documento) o `BM$AUTO<progresivo>` (alta manual sin documento). Los suscriptores que ya existían conservan su código viejo (documento crudo o `AUTOn`) -- no se migran.
- **`udid` + `temp_token`**: pareja de credenciales de un pareo de TV en curso. `udid` (8 hex) identifica el intento; `temp_token` es el secreto real (sin él, adivinar el `udid` no sirve de nada). Los genera la TV al iniciar el pareo y expiran a los 5 minutos si no se completa.
- **`device_token`**: secreto de 32+ bytes que identifica a un dispositivo (TV, celular o navegador web) como "dispositivo vinculado" de la cuenta, independiente del pareo de TV. Lo entrega el backend la primera vez que ese dispositivo se registra en `/ws/device/`; el cliente debe guardarlo de forma persistente y reenviarlo en la próxima conexión para refrescar el registro en vez de crear uno nuevo. **Importante, confirmado con un bug real:** un logout normal (el mismo usuario cerrando sesión en el mismo dispositivo, con intención de volver a entrar después) **no debe borrar este token** -- si el cliente lo hace, el próximo login no tiene nada que reenviar en `register_device` y el backend crea un `DeviceSession` nuevo cada vez, duplicando el dispositivo en la lista en vez de refrescar el mismo registro. Solo corresponde borrarlo cuando cambia el usuario en el mismo dispositivo (evita arrastrar el token de la cuenta anterior), o cuando el propio backend lo invalida (`device_token_invalid`, `device_revoked` -- ver 4.4).
- **Prerrequisito de JWT para login manual: YA RESUELTO.** Las dos guías anteriores marcaban esto como pendiente ("el login manual no pasa por JWT"). La solución que ambas proponían (llamar también a `POST /api/auth/login/` tras el login manual/social/pareo de TV, solo para obtener el JWT que usa "dispositivos vinculados") **ya está implementada del lado cliente en appVideo** (`deviceAuthService.js` + `deviceSessionService.js`, ver sección 3) y no requiere ningún cambio adicional en el backend. Mobile e iOS/Android deben replicar el mismo patrón: dos logins con las mismas credenciales, uno contra PanAccess (contenido) y otro contra `/api/auth/login/` (JWT, solo para las funciones de este documento).

---

## 1. TV (Smart TV -- webOS/Tizen/Android TV/etc.)

### 1.1 Pareo manual/QR (Fase 1)

1. La TV pide un código: `GET /wind/request-udid-manual/`, con header opcional `X-Device-Public-Key` (base64 de una clave pública RSA en PEM generada por la propia TV para este pareo -- la privada nunca sale de la TV). Rate limit: 1 cada 5 minutos por huella de dispositivo (429 `DEVICE_FP_RATE_LIMIT_EXCEEDED`). Respuesta (201): `udid`, `temp_token`, `expires_at`, `expires_in_minutes` (5), `device_fingerprint`, `remaining_requests`. La TV muestra el `udid` (código corto) y guarda el `temp_token` en memoria.
2. La TV se conecta a `ws/auth/` y manda `{"type":"auth_with_udid","udid":"...","temp_token":"...","app_type":"web","app_version":"1.0"}` (`app_type` uno de `web|lg|samsung|android|androidtv|amazon|iOS|iOStv`). Mensajes posibles: `{"type":"pending",...}` (nadie asoció todavía), `{"type":"ping"}` cada 30s (responder `pong`, o se cierra a los 180s de inactividad), `{"type":"timeout",...}` (300s sin completarse, pedir `udid` nuevo), `{"type":"auth_with_udid:result","status":"ok"|"error",...}`.
3. Con `status:"ok"`, el resultado trae `encrypted_credentials` (`encrypted_data`, `encrypted_key`, `iv`, `algorithm`) -- esquema híbrido AES-256-CBC + RSA-OAEP(SHA-256). Si la TV mandó `X-Device-Public-Key` en el paso 1 (recomendado para todo desarrollo nuevo), desencripta `encrypted_key` con su propia clave privada efímera. Si no la mandó, el backend usó la clave RSA estática legada por `app_type` -- vigente solo para compatibilidad con clientes viejos; no usar en integraciones nuevas.

#### 1.1.1 Formato del código y QR que muestra la TV (contrato, agregado 2026-09-02)

Esto no estaba documentado formalmente en ningún lado -- vivía solo como detalle de implementación en `appVideo/src/pages/LoginPage.jsx`, lo que forzaba a cualquier equipo (mobile, TV de terceros) a leer ese código fuente para saber qué esperar. Cierra parcialmente el hallazgo #34 de la auditoría (documentarlo); la otra mitad (si conviene cambiarlo) queda en `docs/PROPUESTA_FORMATO_QR_UDID_2026-09-02.md`.

- **Contenido mostrado en pantalla:** el `udid` tal cual (8 caracteres hex, ej. `a1b2c3d4`) como texto legible, más un QR aparte en la misma área/modal.
- **Payload actual del QR** (generado client-side con la librería `qrcode`, `QRCode.toDataURL()`, nunca lo arma el backend): `"{appName}:{udid}:{temp_token}"` -- por ejemplo `"WindTV:a1b2c3d4:xJ9k2m...`" (el `temp_token` real mide 43 caracteres, `secrets.token_urlsafe(32)`). Si el backend no devolvió `temp_token` (retrocompatibilidad con una versión vieja), el QR cae a `"{appName}:{udid}"` sin el tercer campo.
- **Reglas de parseo para quien lo consuma hoy:** separar por `:` tomando los **últimos dos** segmentos como `udid` y `temp_token` (`rsplit(':', 2)` o equivalente), NO asumir exactamente 3 partes de un split normal -- `appName` es texto de configuración de marca (`useBrand()` en appVideo) y no hay ninguna garantía de que nunca contenga un `:`. `udid` (hex) y `temp_token` (base64 URL-safe) nunca contienen `:`, así que son seguros de aislar por la derecha.
- **Sin campo de versión.** Si este formato cambia, no hay forma de detectarlo desde el payload mismo -- cualquier cambio futuro debe coordinarse explícitamente con quien ya haya programado un parser contra esto.
- **Advertencia explícita:** este formato es un detalle de implementación de `appVideo`, no un estándar acordado con el equipo de TV -- confirmar con ellos antes de programar un parser en mobile, tal como ya advertía la sección 9 de este documento.

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

#### 2.2.1 Comportamiento de UX esperado (qué debe existir en la app, no solo qué API llamar)

El punto de partida real es **el usuario ya logueado en la app, no en una pantalla de login**. Esto no estaba explicitado antes y genera confusión porque el backend describe el paso como "hacer login social" -- pero eso es la implementación del lado del servidor, no lo que la app le debe mostrar a la persona. Flujo esperado:

1. **Tiene que existir un punto de entrada dentro de la app ya logueada** (ej. un botón/menú "Vincular TV" en inicio o ajustes) que abra la cámara para escanear el QR. No es la pantalla de login -- si la app manda al usuario a loguearse de nuevo para esto, la integración está mal hecha.
2. La app decodifica el QR y extrae `udid` + `temp_token` (formato del QR: lo define el equipo de TV, confirmar antes de programar el parseo -- ver pendientes, sección 9).
3. **La app obtiene un token de Google/Facebook de forma silenciosa**, usando la sesión ya activa del SDK (Google Sign-In / Facebook SDK) -- sin mostrarle al usuario ningún prompt de login ni pedirle que vuelva a autorizar nada. Si la sesión del SDK expiró o no hay una activa, ahí sí corresponde pedir que inicie sesión, pero es el caso borde, no el camino normal.
4. Con ese token (silencioso o no), la app llama a `POST /wind/auth/google|facebook/` agregando `udid`+`temp_token` en el body (2.2). La respuesta trae un JWT nuevo -- si el usuario ya tenía uno, se puede simplemente reemplazar/ignorar, es la misma cuenta.
5. La app **no tiene que hacer nada más** salvo mostrar el resultado: éxito (`udid_pairing.ok: true`) → "TV vinculada"; error → mensaje según `udid_pairing.code` (tabla en 2.2), casi siempre "pedí un código nuevo en la TV" porque son de un solo uso y expiran a los 5 minutos.

En resumen, para el usuario todo el proceso debe verse como "tocar un botón, escanear, ver confirmación" -- nunca como un segundo login visible, aunque técnicamente el backend lo procese como tal.

### 2.3 Dispositivos vinculados y 2.4 Password/cuenta

Ver secciones 4 y 5 -- mismo alcance, protocolo y endpoints que el resto de las plataformas.

### 2.5 Alternativa sin escanear QR (auto-servicio web, no requiere trabajo de las apps)

Agregado 2026-09-02, ver `docs/PAREO_UDID_AUTOSERVICIO_CUENTA_2026-09-02.md`. Mientras 2.2 (login social + QR) dependa de que las apps móviles lo implementen, existe un camino alternativo que ya funciona hoy sin tocar ninguna app: el usuario lee el código corto (`udid`, no el QR completo, no necesita `temp_token`) directamente de la pantalla que lo muestra -- TV, celular o navegador, `login.udid` es el mismo componente de `appVideo` en los tres -- y lo escribe en la sección "Vincular dispositivo" del dashboard web de Wind (`POST /wind/associate-udid-by-account/`, requiere sesión JWT en el navegador, sin `temp_token`).

Esto no reemplaza 2.2 ni requiere que mobile haga nada -- es un stopgap independiente, útil para soporte/producto mientras la integración nativa sigue pendiente. Las apps pueden, opcionalmente, mostrar un mensaje tipo "¿no podés escanear? entrá a tu cuenta en la web y escribí el código" apuntando a esta alternativa, pero no es un requisito de esta guía.

---

## 3. Web (appVideo)

### 3.1 Estado real: dispositivos vinculados ya implementado del lado cliente

A diferencia de lo que decían las dos guías anteriores ("ninguna app cliente implementó esto todavía"), en una revisión de `D:\appVideo` se confirmó que **el prerrequisito de JWT y el registro de dispositivo ya están programados**, en:

- `src/services/deviceAuthService.js` -- login contra `/api/auth/login/`, refresh contra `/api/auth/token/refresh/`, y `authorizedDeviceRequest()` (fetch autenticado con reintento automático de refresh en 401). Activación opt-in por brand: `login.deviceSession.enabled` en `brands.js` -- si un brand no lo activa, este módulo no hace ninguna llamada de red.
- `src/services/deviceSessionService.js` -- abre `wss://.../ws/device/?token=<access>`, manda `register_device` (con `device_token` guardado si existe), persiste el `device_token` devuelto en `localStorage` namespaced por brand, reintenta una vez con refresh si el WS cierra por JWT vencido, y expone `closeActiveDeviceSession()` para cerrar la conexión en logout.
- `src/services/loginFlow.js` -- llama a lo anterior (`maybeEstablishDeviceSession`, fire-and-forget) desde `loginAndActivateLicense`, cubriendo login manual, social, TV pareada y reactivación de sesión.

Es una implementación correcta en su diseño de base (persistencia en `localStorage`, reenvío del `device_token` existente para refrescar en vez de duplicar). A partir de una prueba end-to-end real (vincular/revocar dispositivos desde varias ventanas/cuentas) se encontraron y corrigieron 4 gaps concretos, y quedan 2 sin resolver -- ver el detalle completo en la sección 4.4, que aplica a **cualquier** cliente que implemente este mismo contrato, no solo a appVideo.

**Ya corregido en appVideo** (commits de esta integración):

- El push `device_revoked` no tenía ningún efecto visible en producción (`loginFlow.js` solo lo logueaba en consola en DEV) -- ahora `deviceSessionService.js` expone un canal global (`setOnDeviceRevoked`) al que `App.jsx` se suscribe una sola vez al montar la app, y fuerza un logout real + redirect a splash. Ver 4.4.a.
- Un logout normal (mismo usuario, mismo dispositivo) borraba el `device_token`/`id` guardado junto con el resto de la sesión, causando un `DeviceSession` nuevo en cada re-login -- primero se preservó puntualmente en `MiCuentaPage.jsx`, pero el bug se seguía reproduciendo desde otros puntos de logout (`ProfilePage.jsx`, `SmartCardPage.jsx`) y, sobre todo, porque `clearSessionBeforeNewLogin()` (usada al PRINCIPIO de todo intento de login, no solo de logout) volvía a borrarlo sin preservación justo antes de reintentar. Se centralizó la preservación en esa misma función compartida, y se agregó un reintento automático ante `device_token_invalid` para cubrir el caso de un dispositivo compartido entre cuentas distintas. Ver 4.4.b.
- `device_registered` ahora sí devuelve `id` (ver 4.1) y `LinkedDevicesPanel.jsx` lo usa para marcar "este dispositivo" en la lista con una etiqueta y un botón "Cerrar sesión aquí" en vez de "Revocar".
- El marcado de "este dispositivo" se leía una sola vez al montar el panel, antes de que el registro por WebSocket (asíncrono, se dispara al hacer login) terminara -- quedaba con el id vacío para siempre en esa sesión si la pantalla de dispositivos se abría antes de tiempo. Ahora es reactivo (`setOnDeviceRegistered`) y se actualiza solo apenas termina el registro.
- La lista de "dispositivos vinculados" no se enteraba de cambios hechos desde OTRO dispositivo de la misma cuenta (revocar uno desde otra ventana dejaba el conteo viejo hasta apretar "Actualizar") -- ahora reacciona al nuevo mensaje `device_list_changed` (`setOnDeviceListChanged`) y se refresca sola. Ver 4.3 y 4.4.d.

**Todavía sin resolver** (client-side, quedan documentados para cuando se decida tocarlos):

1. **`src/services/splashAuthFlow.js`, líneas 31-41 (`resolveSplashDestination`):** cuando la sesión de PanAccess sigue siendo válida y la licencia se reactiva con éxito, la función retorna directo a la ruta post-login **sin volver a llamar a `loginAndActivateLicense`/`maybeEstablishDeviceSession`**. Es el camino más común de un refresh de pantalla con sesión activa -- en ese caso la app nunca reabre `ws/device/`, así que deja de recibir `device_revoked` en vivo hasta el próximo login real, aunque el `device_token` guardado sigue siendo válido.
2. **`src/services/loginFlow.js`, línea 247:** `maybeEstablishDeviceSession(...)` se llama sin `await`, inmediatamente después de `userSession.setLoggedIn(...)`. El backend crea el `DeviceSession` en el momento en que recibe `register_device` (antes de mandar la confirmación `device_registered`); el cliente solo guarda el `device_token` al recibir esa confirmación. Si la pestaña se recarga o navega en la ventana entre esos dos momentos (login recién hecho, registro todavía en vuelo por la red), el `DeviceSession` ya quedó creado en el backend pero el cliente nunca llegó a persistir su token -- el siguiente intento de registro no tiene `existingToken` para reenviar y crea uno nuevo.

Ninguno de los dos es un bug del backend -- el backend hace exactamente lo que debe (crear si no le llega `device_token`, refrescar si le llega uno válido).

### 3.2 Resto del alcance

Mismo que mobile para password/cuenta (sección 5). Web normalmente no inicia pareos de TV, pero podría usar la misma sección 2.2 si el negocio lo requiere.

---

## 4. Dispositivos vinculados (protocolo común a TV, mobile y web)

### 4.1 Registro por WebSocket

`ws/device/?token=<jwt de access>`. El JWT va como query param (no hay forma de mandar headers en el handshake WS desde algunas plataformas). Cierres: **4001** (JWT inválido/expirado, o límite de conexiones excedido -- viene con motivo y segundos de espera), **4004** (JWT válido pero no se pudo resolver `subscriber_code`).

Mensaje para registrarse: `{"type":"register_device","device_type":"...","device_model":"...","device_token":"<opcional, el guardado la vez anterior>"}`. `device_type` recomendado: `iOS`, `android`, `web`, `lg`, `samsung` (cualquier otro string se acepta igual, truncado a 50 caracteres, pero conviene usar estos para que el dashboard filtre bien).

Respuesta: `{"type":"device_registered","id":<int>,"device_token":"<token>","is_new":true|false}`. Guardar tanto `device_token` como `id` de forma persistente y segura (Keychain/Keystore en mobile, `localStorage` en web) y reenviar `device_token` en la próxima conexión para refrescar en vez de crear un registro nuevo. El `id` es el mismo que aparece en `GET /wind/devices/` (4.2) -- sirve para que el propio cliente marque "este dispositivo" en su lista sin heurísticas (ver 4.4.c). Límite: 20 dispositivos **nuevos** por hora por suscriptor (refrescar uno existente no cuenta). Si se excede: `{"type":"error","code":"rate_limited",...}` y cierre 1011. Si el `device_token` enviado no es válido (revocado o de otra cuenta): `{"type":"error","code":"device_token_invalid",...}` y cierre 1011 -- tratar igual que una revocación (borrar el token local, registrarse de nuevo sin él).

`ping`/`pong`: el servidor manda `{"type":"ping"}` cada 30s, el cliente responde `{"type":"pong"}`.

### 4.2 Listar y revocar (REST, JWT)

`GET /wind/devices/` → `{"devices": [{"id","device_type","device_model","first_seen_at","last_seen_at","client_ip","country","city"}, ...]}` (el `device_token` nunca se expone acá). `country`/`city` son una ubicación **aproximada** resuelta desde `client_ip` -- puramente informativa para que el usuario reconozca sus propios dispositivos en la lista, nunca debe usarse para ningún control de acceso ni decisión de seguridad. Pueden venir en `null` (ambos, nunca uno solo) si la IP es privada/inválida, si no se encontró en ninguna fuente, o si el backend no tiene ninguna fuente configurada (ver sección 10) -- los clientes deben ocultar el dato en vez de mostrar un "—" cuando ambos sean `null`.

Dos fuentes, en orden (`wind/utils/geo_lookup.py`): (1) una base local `.mmdb` (GeoLite2-City de MaxMind, o DB-IP City Lite -- gratuita y sin cuenta, mismo formato/esquema), consultada primero, sin ninguna llamada de red; (2) si esa no encontró nada para la IP (caso raro), un fallback opcional a **ip-api.com Pro** (requiere `IP_API_KEY`, provista por el cliente) -- con timeout corto, nunca bloquea la respuesta si ip-api está lento o caído.

`POST /wind/devices/<id>/revoke/` → éxito `{"ok": true}` (200); errores `{"ok": false, "code": "not_found"}` (404, mismo resultado si el id no existe o es de otra cuenta -- a propósito, para no filtrar información), `{"ok": false, "code": "already_revoked"}` (409), `{"ok": false, "code": "subscriber_unresolved"}` (400). Un `{"ok": true}` con 200 es la confirmación completa: significa que el `DeviceSession` quedó en `status="revoked"` en la base y que se programó el aviso en vivo por WebSocket -- no hace falta ninguna otra llamada para confirmar el efecto, aunque si se quiere verificar explícitamente, un `GET /wind/devices/` posterior ya no debe listar ese `id`. Límite: 60 solicitudes/minuto.

### 4.3 Notificación push de revocación

Cuando un dispositivo se revoca (dashboard, o en bloque por cambio de contraseña/cierre de cuenta, sección 5), su WS recibe `{"type":"device_revoked","reason":"revoked_by_subscriber"|"password_changed"|"account_closed"}` y el backend cierra la conexión. El cliente debe borrar su `device_token` local y forzar login de nuevo. Si el dispositivo no está conectado en ese momento, el efecto se aplica igual la próxima vez que intente reconectarse o refrescar su registro (el `device_token` ya no sirve).

**Aviso relacionado, distinto: `device_list_changed`.** Este SÍ le llega a los DEMÁS dispositivos conectados de la misma cuenta (no al que se revocó/registró) -- `{"type":"device_list_changed"}`, sin `reason` ni ningún otro dato. Se dispara cuando se revoca un dispositivo (`POST /wind/devices/<id>/revoke/`) o cuando se registra uno **nuevo** (`register_device` con `is_new:true`) -- NO cuando solo se refresca uno existente. A diferencia de `device_revoked`, este mensaje no cierra la conexión ni implica que el dispositivo que lo recibe perdió acceso a nada: es solo una señal de "la lista de dispositivos de tu cuenta cambió, volvé a pedir `GET /wind/devices/` si la tenés abierta en pantalla en este momento". Ver 4.4.d para el motivo por el que se agregó.

### 4.4 Casos de uso de "desvincular dispositivo" y lecciones de una integración real

Lo que sigue sale de vincular/revocar dispositivos de punta a punta con varias cuentas y ventanas reales (no solo lectura de código) -- aplica a **cualquier** cliente que implemente este contrato, TV, mobile o web, no es específico de appVideo.

**Los 5 casos de uso posibles** (matriz de quién inicia la revocación × si el dispositivo objetivo está conectado en ese momento, más el caso especial de autorrevocación):

| # | Quién revoca | Objetivo | Qué recibe quien revoca | Qué recibe/debe hacer el objetivo |
|---|---|---|---|---|
| 1 | Backend/dashboard admin | Otro dispositivo, conectado | `{"ok": true}` (REST) | Push `device_revoked` en vivo por su propio WS -- debe reaccionar de inmediato (ver 4.4.a) |
| 2 | La propia app del usuario | Otro dispositivo suyo, conectado | `{"ok": true}` (REST) | Igual que el caso 1 |
| 3 | La propia app del usuario | Otro dispositivo suyo, apagado/desconectado | `{"ok": true}` (REST) -- no depende del estado del objetivo | Nada en el momento (no hay nadie escuchando); se aplica solo en el próximo intento de reconexión/refresco, con `{"type":"error","code":"device_token_invalid",...}` |
| 4 | El propio dispositivo que se está usando (autorrevocación) | Sí mismo | `{"ok": true}` (REST) **y además** el push `device_revoked` por su propio WS, casi al mismo tiempo -- tratar ambos como la misma acción, no como una revocación "sorpresa" | Debe reaccionar igual que el caso 1/2 (es el mismo mecanismo, solo que el iniciador y el objetivo son el mismo dispositivo) |
| 5 | Backend/dashboard admin | Otro dispositivo, apagado/desconectado | `{"ok": true}` (REST) | Igual que el caso 3 |

Aparte de estos 5, existe una **revocación en bloque** (no elige un `id`): cambio/recuperación de contraseña y cierre de cuenta (sección 5) revocan **todos** los dispositivos de la cuenta de una sola vez, con `reason="password_changed"` o `"account_closed"` en vez de `"revoked_by_subscriber"` -- mismo push, mismo manejo esperado del lado del cliente.

**a) El push `device_revoked` tiene que forzar una reacción real, no solo limpiar storage.** Es el bug más importante que se encontró: es fácil implementar el WS para que, al recibir `device_revoked`, borre el `device_token` guardado y cierre el socket -- pero si ahí se queda, el usuario sigue viendo la app como si nada, logueado, con una sesión que el backend ya considera terminada. La reacción esperada es la misma que un logout forzado: limpiar toda la sesión local (no solo el `device_token` de "dispositivos vinculados" -- también las credenciales/sesión del sistema de contenido, si son independientes, como en appVideo con PanAccess) y redirigir a la pantalla de login. Si el framework del cliente separa la lógica de red (servicios planos) de la capa de UI (React, etc.), conviene un mecanismo de notificación global (evento, pub/sub, callback registrado al nivel más alto de la app) para que la capa de red pueda pedirle a la capa de UI que navegue, en vez de journalear el evento y no hacer nada más.

Esta misma lección aplicó también al portal de suscriptor (`wind/templates/wind/dashboard.html`, servido por el propio backend): su primera versión cerraba el WebSocket apenas terminaba de registrarse (razonamiento original: "es una página, no una app de uso continuo, no vale la pena mantenerlo vivo"), así que nunca tenía ningún canal abierto para enterarse de un `device_revoked` -- una autorrevocación hecha desde otra pantalla (appVideo, otra pestaña) dejaba esa pestaña del dashboard con la sesión "zombie", viéndose como activa indefinidamente. Se corrigió manteniendo el socket abierto mientras la pestaña siga abierta y agregando el mismo manejo de `device_revoked` (logout real + redirect). Cualquier cliente "de página" (no solo SPAs de uso continuo) que implemente `ws/device/` debe considerar este mismo caso antes de decidir cerrar el socket después de registrarse.

**b) Un logout normal NO debe borrar el `device_token` -- y esto hay que aplicarlo en UN SOLO lugar central, no en cada pantalla que haga logout.** Primera vuelta de este bug: si el logout limpia todo el storage sin distinguir "el mismo usuario se va a volver a loguear en este mismo dispositivo" de "otro usuario va a entrar en este dispositivo", el primer caso pierde su `device_token` sin necesidad -- el siguiente login no tiene nada que reenviar en `register_device`, y el backend crea un `DeviceSession` nuevo cada vez, acumulando duplicados del mismo dispositivo físico en la lista.

El primer intento de arreglo (appVideo) preservó/restauró el `device_token`/`id` en el único botón de logout que se había revisado (`MiCuentaPage.jsx`) -- y **el bug siguió reproduciéndose**, porque la app tenía otros puntos de logout (`ProfilePage.jsx`, `SmartCardPage.jsx`, ambos con su propio `handleBack()` sin la preservación) y, más importante todavía, una función compartida (`clearSessionBeforeNewLogin()`, usada al PRINCIPIO de todo intento de login, no solo de logout) que volvía a aplicar el borrado sin preservación justo antes de reintentar el login -- pisando cualquier preservación que un logout anterior hubiera hecho bien. Diagnóstico real de un caso de prueba: 2 dispositivos vinculados → cerrar sesión en uno → volver a entrar en el mismo → 3 dispositivos vinculados (uno de más).

La solución no es repetir el parche en cada pantalla, sino centralizarlo: mover la preservación a la función compartida que ya usan todos los caminos de logout/pre-login (`clearSessionBeforeNewLogin()`), y no intentar adivinar ahí si el próximo login va a ser del mismo usuario o de uno distinto -- esa decisión ya la toma el backend solo: `_register_or_refresh_device()` rechaza con `device_token_invalid` cualquier token que no pertenezca al `subscriber_code` que se está autenticando o que ya esté revocado, así que preservar el token "a ciegas" es seguro incluso en un dispositivo compartido entre cuentas distintas. Para que ese caso no quede sin ningún dispositivo registrado hasta el próximo login, el cliente agrega un único reintento automático apenas recibe `device_token_invalid`: reabre la conexión sin `device_token` (ya se limpió localmente al recibir el error) en el mismo intento, en vez de rendirse hasta la próxima vez.

**c) Marcar "este dispositivo" en la lista es una lectura que debe ser reactiva, no puntual.** El registro por WebSocket (`register_device`, que entrega el `id`/`device_token` de este dispositivo) se dispara en paralelo al login, sin que nada lo espere -- puede tardar más que un simple `GET` a la lista de dispositivos. Si el componente/pantalla que muestra la lista lee el `id` propio guardado **una sola vez** al montarse (por ejemplo, una constante calculada al render inicial), puede quedarse con un valor vacío para siempre en esa sesión si el usuario abre esa pantalla antes de que el registro termine -- aunque el registro se complete un instante después. La lectura debe repetirse (estado reactivo + suscripción a un evento de "registro completado", o cualquier mecanismo equivalente) para no depender de qué tan rápido terminó una carrera en segundo plano.

**d) La lista de dispositivos de los DEMÁS dispositivos conectados también necesita un canal de aviso, no solo el dispositivo afectado.** `device_revoked` (4.3) le llega únicamente al dispositivo que se revocó -- correcto para forzarle el logout, pero insuficiente para lo demás: si dos dispositivos de la misma cuenta tienen el panel de "dispositivos vinculados" abierto a la vez y uno revoca al otro, el que se queda con la sesión activa seguía viendo el conteo viejo (ej. "2 dispositivos") indefinidamente, porque nada le avisaba que la lista había cambiado -- solo se enteraba si alguien apretaba "Actualizar" a mano o recargaba la pantalla. La causa de fondo es que el backend solo tenía un grupo de canal *por dispositivo* (`device_{token}`), no uno *por cuenta*. Se agregó un segundo grupo (`subscriber_devices_{subscriber_code}`) al que se une cada conexión `ws/device/` además del suyo propio, y un mensaje nuevo y liviano, `{"type":"device_list_changed"}` (ver 4.3), que se manda a ese grupo compartido cuando se revoca un dispositivo o se registra uno nuevo -- sin cerrar ninguna conexión ni implicar ninguna acción destructiva para quien lo recibe, solo "refrescá tu lista si la tenés abierta".

---

## 5. Password y cuenta (común a las tres plataformas)

### 5.1 Cambiar contraseña

`POST /api/v1/profile/password/` (JWT), body `{"code": "<subscriber_code>", "oldPass": "...", "newPass": "..."}` → `{"success": true, "message": "Contraseña actualizada"}`.

**`oldPass` es obligatorio desde 2026-08-28 (cambio de contrato, actualizar si tu integración quedó armada antes de esa fecha).** Es la contraseña actual del suscriptor; el backend la verifica contra PanAccess antes de aplicar el cambio. Motivo: sin esto, un JWT robado/filtrado alcanzaba por sí solo para cambiar la contraseña (y de paso expulsar al dueño real de la cuenta, ya que el cambio revoca todas las demás sesiones -- ver más abajo). Si tu pantalla de cambio de contraseña no le pide la contraseña actual al usuario todavía, hay que agregar ese campo antes de llamar a este endpoint -- sin él, el backend rechaza el request con 400 de validación (`oldPass` faltante), no deja pasar el cambio.

**Política de contraseña (`newPass`):** entre 8 y 255 caracteres, al menos una letra mayúscula, al menos un número, y solo caracteres `a-z`, `A-Z`, `0-9`, `-`, `_` y especiales `! @ # $ % ^ & * ( ) + = [ ] { } ; : ' " , . < > / ? ~ ` | \`. El cliente debería validar esto localmente antes de llamar al endpoint para evitar el round-trip en el caso obvio.

**Nota sobre los caracteres especiales:** el charset base (`a-z`, `A-Z`, `0-9`, `-`, `_`) está confirmado porque es literalmente lo que devuelve PanAccess en su mensaje de error. Los caracteres especiales se agregaron a la validación local del backend, pero todavía no está confirmado que PanAccess los acepte también -- si PanAccess los rechaza, la respuesta es 400 con `code=password_rejected_by_panaccess` (ver tabla abajo), no un error. Tratar como pendiente de confirmar hasta correr `deploy/test_password_policy_probe.py` con un caracter especial.

**Errores:** el body de error siempre incluye `"success": false` y `"message"` (texto legible); cuando corresponde, además incluye un campo `"code"` estable para no depender de parsear `message`:

| Status | `code` | Significado |
|---|---|---|
| 400 | `old_password_incorrect` | `oldPass` no coincide con la contraseña actual real. Mostrar mensaje específico ("tu contraseña actual no es correcta"), no un error genérico. |
| 400 | `password_policy_violation` | `newPass` no cumple la política de arriba (detectado localmente, sin llamar a PanAccess). |
| 400 | `password_rejected_by_panaccess` | PanAccess respondió y rechazó la contraseña por una regla no cubierta por la validación local. Incluye además `"panaccess_error_code"` (puede ser `null`). |
| 429 | `old_password_locked` | Demasiados `oldPass` incorrectos seguidos (5 intentos, ventana de 5 min) -- bloqueado 15 min. Distinto del `rate_limited` general: este es específico de intentos fallidos de la contraseña actual. |
| 429 | `rate_limited` | Demasiados intentos; reintentar más tarde. |
| 502 | `panaccess_integration_error` | Problema de la sesión/credencial de servicio con PanAccess -- no es culpa del valor enviado. |
| 503 | `panaccess_unavailable` | PanAccess no respondió (conectividad). |
| 504 | `panaccess_timeout` | PanAccess tardó demasiado en responder. |

Antes de este fix, cualquiera de estos casos (incluida la violación de política) se devolvía como 502 sin `code`, indistinguible de una falla real de PanAccess -- si el cliente ya tenía lógica especial para 502, debe actualizarla para tratar 400 con `code=password_policy_violation`/`password_rejected_by_panaccess`/`old_password_incorrect` como error de input corregible, no como "reintentar más tarde".

**Efecto colateral automático (solo en éxito):** invalida todos los JWT ya emitidos y revoca **todos** los `DeviceSession` de la cuenta -- incluida la propia sesión que hizo el cambio. Tras un cambio exitoso, la app debe volver a loguearse (JWT nuevo) y volver a registrar el dispositivo (sección 4.1).

### 5.2 Contraseña olvidada

`POST /api/auth/password/forgot/` (`email`, `recaptcha_token`) → siempre 200 genérico. `POST /api/auth/password/reset-confirm/` (`token`, `newPass`, `confirmPass`) → 200 si aplicó.

`newPass` sigue la misma política de contraseña de la sección 5.1 (incluida la validación local), y los errores siguen el mismo contrato de `code`/status:

| Status | `code` | Significado |
|---|---|---|
| 400 | `password_policy_violation` | `newPass` no cumple la política (validado localmente). |
| 400 | `password_rejected_by_panaccess` | PanAccess rechazó la contraseña por una regla no cubierta localmente. Incluye `"panaccess_error_code"`. |
| 400 | -- (`error_type`: `TokenExpired`/`TokenUsed`/`InvalidToken`) | El enlace es inválido, expiró o ya se usó. |
| 429 | `rate_limited` | Demasiados intentos. |
| 502 | `panaccess_integration_error` | Problema de sesión/credencial de servicio con PanAccess. |
| 503 | `panaccess_unavailable` | PanAccess no respondió. |
| 504 | `panaccess_timeout` | PanAccess tardó demasiado. |

Antes de este fix, un rechazo por política de contraseña en este flujo salía como 502 con un mensaje genérico que descartaba el motivo real (peor que 5.1, que al menos devolvía el texto de PanAccess) -- si el cliente tiene lógica especial para ese caso, actualizarla igual que en 5.1.

Mismo efecto colateral que 5.1 (solo en éxito).

### 5.3 Cerrar cuenta

`POST /api/v1/profile/account/close/` (JWT + reCAPTCHA), body `{"code","confirm" (=code),"reason","dry_run"}`. `dry_run:true` no borra nada, solo devuelve un plan. Cierre exitoso: `{"success":true,"subscriber_code","panaccess":{...},"local":{...,"device_sessions_revoked":N,"udid_revoked":N},"closure_log_id","re_registration":"allowed_without_trial",...}`. Cierre parcial (PanAccess falló pero el acceso local ya se cortó): `{"success": false, ...}` -- el corte de acceso local (JWT, dispositivos, pareos) ocurre siempre, incluso si PanAccess no terminó.

---

## 6. Preferencias sincronizadas (control parental + favoritos)

Funcionalidad nueva (2026-08-31), independiente de "dispositivos vinculados" (sección 4) aunque comparte el mismo JWT de sesión. Sincroniza entre todos los dispositivos de una cuenta el control parental **propio de la app** (PIN local, canales bloqueados, clasificación por edad) y la lista de canales favoritos -- antes vivían solo en almacenamiento local de cada dispositivo, sin compartirse entre ellos. **No tiene nada que ver** con el PIN de perfil de PanAccess (ese viene de la smartcard, se lee automático y el usuario nunca lo escribe) -- son dos sistemas de PIN totalmente separados.

### 6.1 Prerrequisito

Solo hace falta el JWT de sesión de la sección 0 -- el mismo que se obtiene con `POST /api/auth/login/` (o login social). **No depende de "dispositivos vinculados":** no hace falta abrir `ws/device/` ni tener un `device_token` registrado para usar este endpoint, son features independientes que solo comparten el mismo JWT. (Aclaración porque en appVideo-web hoy ese JWT solo se obtiene cuando `login.deviceSession.enabled` está activo por brand -- eso es una decisión de implementación de ese cliente puntual, no una restricción del backend. Mobile/iOS/Android pueden llamar a este endpoint apenas tengan el JWT de la sección 0, sin necesidad de implementar dispositivos vinculados.)

### 6.2 `profileKey`: cómo se enlaza cada dispositivo al perfil correcto

Cada cuenta puede tener varios perfiles tipo Netflix, administrados enteramente por PanAccess (no por este backend). La preferencia se guarda por el par `(subscriber_code, profileKey)`:

- Si la cuenta **tiene un perfil PanAccess activo/elegido**, `profileKey` = el mismo identificador de perfil que el cliente ya usa para las llamadas a PanAccess.
- Si la cuenta **no tiene perfiles activados, o todavía no se eligió ninguno**, `profileKey` = el string literal `"default"` (o se omite -- el backend usa `"default"` por omisión).

El `subscriber_code` nunca lo manda el cliente -- se resuelve en el backend a partir del JWT, igual que el resto de la API (sección 0). Dos cuentas distintas usando `"default"` no se cruzan nunca: la clave real siempre es el par completo, no `profileKey` aislado.

**Migración automática (una sola vez):** la primera vez que una cuenta usa un `profileKey` real (no `"default"`), el backend copia automáticamente lo que había bajo `"default"` hacia ese perfil nuevo, para no perder la config que el usuario ya tenía antes de activar perfiles. Cualquier perfil real siguiente arranca vacío -- cada persona de la cuenta configura lo suyo. Es transparente para el cliente: no hay que hacer nada especial, solo mandar el `profileKey` correcto en cada llamada.

### 6.3 Leer preferencias guardadas

`GET /api/v1/preferences/?profileKey=<opcional>` (JWT). Sin `profileKey`, usa `"default"`.

Respuesta 200:

```json
{
  "success": true,
  "profileKey": "default",
  "parental": {
    "enabled": true,
    "pinHash": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a1",
    "pinSalt": "3f7a1c9e8b2d4560",
    "pinMethod": "pbkdf2",
    "pinIterations": 100000,
    "blockedChannelIds": ["45", "112"],
    "ratingEnabled": true,
    "ratingAllowedMax": 13,
    "ratingApplyToLive": true
  },
  "favorites": ["101", "202", "310"]
}
```

`parental` viene `null` si la cuenta nunca guardó control parental (primer acceso); `favorites` viene `[]` si nunca guardó favoritos -- el cliente debe tratar ambos casos como "sin configurar todavía", no como error.

**Importante -- qué NO trae `parental`:** solo la configuración durable (PIN, canales bloqueados, clasificación). Nunca incluye estado de desbloqueo temporal (por ejemplo "desbloqueado por 30 minutos") -- eso es intencionalmente local a cada dispositivo, para que un desbloqueo hecho en un dispositivo no desbloquee sin querer el control parental en otro. Si tu app maneja un PIN propio (distinto al de PanAccess), guardá el estado de "sesión desbloqueada" solo en local, nunca lo mandes a este endpoint.

### 6.4 Guardar un cambio

`PUT /api/v1/preferences/` (JWT), body con **solo los campos que cambiaron** -- es una actualización parcial: mandar `favorites` no borra `parental` ya guardado, y viceversa.

```json
{
  "profileKey": "default",
  "favorites": ["101", "202", "310", "415"]
}
```

Respuesta 200: el estado completo actualizado, mismo formato que 6.3 (incluye también lo que no cambió, sin tocarlo).

**Validación (400, body `{"success": false, "errors": {...}}`):**

| Campo | Regla |
|---|---|
| `parental` | Debe ser un objeto JSON (no array, string ni número). Tamaño máximo ~20KB. |
| `favorites` | Lista de strings. Máximo 500 elementos. |
| `profileKey` | Opcional; en blanco o ausente cae a `"default"`. |

Otros errores: 404 `{"success": false, "message": "No hay suscriptor vinculado a este usuario."}` si el JWT es válido pero no resuelve `subscriber_code` (mismo caso que el resto de `/api/v1/profile/...`). Rate limit: mismo `ProfileThrottle` que el resto de `/api/v1/profile/...`.

### 6.5 Cuándo llamar a cada uno (patrón recomendado -- no es parte del contrato del backend)

Esto es lo que ya implementa appVideo-web; cualquier cliente nuevo puede replicarlo o adaptarlo:

- **Push (`PUT`) inmediato** cada vez que el usuario cambia algo -- no hace falta agrupar cambios, cada toggle/guardado dispara su propio `PUT`. Es fire-and-forget: si falla (sin red, JWT vencido, etc.) no debe bloquear ni revertir el cambio local, que ya se guardó antes de llamar al backend -- alcanza con reintentar en el próximo evento de sync.
- **Pull (`GET`) en dos momentos:** al iniciar/reanudar la app (para traer lo último guardado por otro dispositivo) y al volver de segundo plano (background → foreground). **No hay push en tiempo real** del backend hacia otros dispositivos conectados en ese momento -- a diferencia de "dispositivos vinculados" (sección 4), este endpoint no tiene canal WebSocket. Si dos dispositivos de la misma cuenta están abiertos en primer plano a la vez, el cambio hecho en uno no le llega al otro hasta que ese otro pase por background/reapertura.

---

## 7. Logs de diagnóstico para desarrolladores

Funcionalidad nueva (2026-09-01): las apps pueden reportar errores/crashes al backend para que el equipo los revise sin depender de que el suscriptor los reporte manualmente. **No es telemetría de negocio ni analítica de uso** -- es diagnóstico técnico puro (ver `docs/LOGS_DIAGNOSTICO_2026-09-01.md` para el detalle completo del lado backend). Pensada especialmente para que **iOS/Android la implementen desde el día uno**, ya que ese contrato no depende de nada que hoy solo exista en appVideo-web.

### 7.1 Prerrequisito: ninguno (a propósito)

A diferencia de todo lo demás en este documento, este endpoint **no requiere JWT** -- el caso de uso central es justamente capturar errores que ocurren antes de poder loguearse (una pantalla de login rota, por ejemplo). Si hay un JWT válido disponible, mandarlo igual (ver 7.2) para que el reporte quede asociado al suscriptor; si no hay uno, o está vencido, el reporte se manda igual y queda sin asociar.

Lo que sí es obligatorio es una **API key propia de la integración** (no es un JWT, no expira, no es por usuario) -- pedir este valor al equipo de backend antes de integrar. Sin ella, o con una incorrecta, el endpoint devuelve 401 sin importar el resto del body.

### 7.2 Enviar un reporte

`POST /api/v1/logs/`, header `X-App-Log-Key: <api key de la integración>`, y opcionalmente `Authorization: Bearer <jwt>` si hay una sesión activa (mismo JWT de la sección 0 -- no hace falta "dispositivos vinculados" activo, son features independientes).

Body:

```json
{
  "platform": "ios",
  "level": "error",
  "message": "TypeError: no se pudo cargar el EPG",
  "stack": "TypeError: ...\n  at EpgLoader.swift:42",
  "breadcrumbs": [
    { "category": "nav", "message": "abrió BouquetPage" },
    { "category": "http", "message": "GET /api/v1/epg -> 500" }
  ],
  "appVersion": "2.4.0",
  "deviceType": "ios"
}
```

| Campo | Obligatorio | Notas |
|---|---|---|
| `platform` | Sí | Uno de: `web`, `tv_tizen`, `tv_webos`, `ios`, `android`. |
| `level` | No | `error` (default), `warning`, `info`. |
| `message` | Sí | Hasta 2000 caracteres. |
| `stack` | No | Hasta 8000 caracteres. |
| `breadcrumbs` | No | Lista de objetos libres (máx. 100) -- lo último que pasó antes del error: navegación, llamadas de red, acciones del usuario. No hay un shape fijo por campo, pero se recomienda al menos `category`/`message` para que sea legible en el panel. |
| `extra` | No | Objeto libre, hasta ~20KB serializado -- cualquier contexto adicional puntual. |
| `appVersion` | No | Versión de la app que reporta. |
| `deviceType` | No | Texto libre descriptivo del dispositivo (modelo, SO, etc.). |

Respuesta 201: `{"success": true}`. Errores: 401 (`X-App-Log-Key` ausente o incorrecta), 400 con `{"success": false, "errors": {...}}` (validación del body), 429 (rate limit -- 30/minuto por defecto, mismo criterio de "no bloquear ni reintentar en loop" que el resto del documento).

### 7.3 Patrón recomendado del lado cliente (no es parte del contrato del backend)

- **Buffer local + envío solo si hay error** (el mismo patrón que ya usa appVideo-web en `errorReporting.js`): mantener en memoria un ring buffer chico (~50 entradas) de "breadcrumbs" -- navegación, llamadas de red, acciones del usuario -- y adjuntarlas recién cuando ocurre un error real. No hace falta mandar nada mientras la app funciona bien.
- **Nunca debe bloquear ni romper la app**: si el envío falla (sin red, endpoint caído), descartar o guardar para reintentar más tarde -- el reporte de diagnóstico nunca debe generar un error nuevo ni afectar la experiencia del usuario.
- Deduplicar en el cliente antes de mandar (mismo mensaje+contexto repitiéndose en loop) para no gastar el rate limit en la primera ráfaga de un error que se repite -- el backend igual agrupa por fingerprint del lado servidor, pero evitar el envío redundante ahorra red en el dispositivo.

---

## 8. Tabla resumen de endpoints

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
| Leer preferencias sincronizadas (parental + favoritos) | `GET /api/v1/preferences/?profileKey=...` | JWT |
| Guardar preferencias sincronizadas | `PUT /api/v1/preferences/` | JWT |
| Reportar un log de diagnóstico | `POST /api/v1/logs/` | API key (`X-App-Log-Key`); JWT opcional |

---

## 9. Manejo de errores y rate limiting (aplica a todo lo anterior)

- Los endpoints de pareo devuelven 429 con `retry_after` (segundos) -- respetar ese tiempo, no reintentar en loop inmediato.
- Ningún endpoint documentado devuelve el detalle crudo de una excepción interna (`str(e)`) -- los mensajes de error son genéricos por diseño; reportar el `code`/`error_type` recibido, no parsear texto libre.
- Todos los `temp_token` se comparan en tiempo constante en el backend -- transportarlos tal cual, sin recortar ni cambiar mayúsculas/minúsculas.

---

## 10. Pendientes / decisiones abiertas (estado a 2026-07-28)

**Ya resueltos** (se listaban acá en versiones anteriores de este documento, quedan solo como referencia de qué cambió):

- Cifrado híbrido sin AEAD -- `crypto_tv.py` ahora soporta un modo autenticado (AES-256-GCM, parámetro `use_aead`) para el esquema nuevo (`hybrid_encrypt_for_device_public_key`, sin clientes en vivo todavía); el esquema legado (`hybrid_encrypt_for_app`, usado hoy por el cliente en producción "cableatlantico") se dejó **intacto a propósito** (AES-256-CBC sin AEAD) para no romper compatibilidad con ese cliente ya desplegado.
- Rate limiting de UDID/WebSocket no atómico -- `check_udid_rate_limit`, `check_temp_token_rate_limit`, `check_device_fingerprint_rate_limit` y `check_token_bucket_lua` ahora usan reserva atómica (`cache.add()`/`cache.incr()` con reintento acotado ante expiración del key a mitad de operación).
- `check_websocket_limits` fail-open -- ahora fail-closed: si Redis falla, rechaza la conexión (`code="rate_limit_unavailable"`, reintentable) en vez de dejarla pasar, revirtiendo cualquier incremento parcial ya aplicado antes del fallo. (El fingerprint evadible por rotación de headers sigue siendo una limitación estructural conocida, sin cambio -- no hay una solución de bajo riesgo pendiente para eso.)
- `UDIDAuthRequest.attempts_count` -- el incremento se movió de cada *consulta de estado* (`GET /wind/validate/`, polling) a cada *intento real de asociación* (`UDIDAssociationSerializer.validate()`), así que el polling normal ya no agota el cupo de 5 intentos.
- Orden lexicográfico en `subscriber_code_generator.py` -- `generate_unique_subscriber_code()` ahora compara numéricamente el sufijo de cada código existente, no como texto.
- `client_ip` en WS sin filtro de proxy confiable -- `device_consumers.py`/`consumers.py` ahora usan `get_client_ip_from_scope()`, que aplica el mismo filtro de `TrustedProxyConfig.TRUSTED_PROXIES` que ya existía para HTTP.
- `device_registered` sin `id` -- ver sección 4.1, ya devuelve `id` y se usa en appVideo para marcar "este dispositivo" (sección 3.1, 4.4.c).
- **`oldPass` obligatorio al cambiar contraseña (2026-08-28)** -- ver sección 5.1. Cambio de contrato: cierra el hueco de que un JWT robado alcanzara por sí solo para cambiar la contraseña sin conocer la actual.
- **Preferencias sincronizadas (control parental + favoritos) -- implementado 2026-08-31**, ver sección 6 y `docs/SINCRONIZACION_PREFERENCIAS_2026-08-31.md`. Queda documentado como limitación conocida, no como bug: sin push en tiempo real entre dispositivos abiertos simultáneamente en primer plano (solo sincroniza al iniciar/reanudar la app) -- si el producto lo requiere a futuro, se puede agregar un aviso liviano por el mismo canal WebSocket de dispositivos vinculados (`subscriber_devices_{subscriber_code}`, sección 4.4.d), sin necesidad de un canal nuevo.
- **Logs de diagnóstico para desarrolladores -- implementado 2026-09-01**, ver sección 7 y `docs/LOGS_DIAGNOSTICO_2026-09-01.md`. Del lado backend: falta generar `APP_LOGS_INGEST_KEY` real en el `.env` del servidor (hoy vacío -- el endpoint rechaza todo hasta configurarlo) y definir `APP_LOGS_ALERT_RECIPIENTS`. Del lado cliente: **appVideo-web todavía no lo implementa** (queda pendiente extender `errorReporting.js` con breadcrumbs y apuntarlo a este endpoint); iOS/Android pueden implementarlo desde cero directo contra el contrato de la sección 7.
- **`DeviceSession` sin expiración (2026-08-28)** -- ahora una tarea Celery diaria revoca automáticamente cualquier sesión de dispositivo sin actividad (`last_seen_at`) hace más de 183 días (`DEVICE_SESSION_IDLE_EXPIRY_DAYS`, ajustable). Un dispositivo perdido/vendido/olvidado ya no queda "de confianza" indefinidamente.
- **appVideo -- los 2 gaps client-side (revisados 2026-08-28), sin acción pendiente:** el de `ws/device/` ya está resuelto (no en `splashAuthFlow.js` puntual, sino con un watchdog centralizado en `useAppLifecycle.js` que garantiza la conexión sin importar el camino de login/reactivación); el de `loginFlow.js`/`device_token` es diseño intencional (fire-and-forget a propósito, para que el registro de dispositivo nunca pueda bloquear ni tumbar el login real), y el único consumidor (`LinkedDevicesPanel.jsx`) ya se actualiza por evento en vez de asumir que está listo de entrada.

**Todavía abiertos:**

- **Password en texto plano** en el correo de bienvenida y en la respuesta de login social -- decisión de negocio ya aceptada, se re-lista solo porque sigue siendo una superficie de exposición real.
- **Fingerprint de dispositivo evadible** rotando los headers que lo derivan (server-side, ya no lo declara el cliente, pero sigue sin ser una huella robusta) -- limitación estructural, sin una solución de bajo riesgo identificada.
- **Site-key de reCAPTCHA para mobile**: todavía sin definir cuál usar en "olvidé mi contraseña" / "eliminar cuenta" desde iOS/Android.
- **Brand `bromteck` en `appVideo`** sigue apuntando a `http://` en vez de `https://` -- error menor, pendiente de que el equipo de `appVideo` lo resuelva.
- **`country`/`city` en `GET /wind/devices/` requieren un paso de despliegue manual en CADA servidor** (local ya resuelto, falta replicarlo en el servidor real de producción): la base `.mmdb` (DB-IP City Lite, gratuita y sin cuenta -- ver 4.2) y la variable `GEOIP_CITY_DB_PATH` viven **fuera de git a propósito** (`.gitignore`), así que un deploy normal no las lleva solas. Pasos en el servidor real: (1) descargar el `.mmdb` mensual y guardarlo en una ruta que sobreviva a futuros deploys (fuera de la carpeta del repo que se pisa con cada `git pull`), (2) definir `GEOIP_CITY_DB_PATH` en el `.env` de ESE servidor apuntando a esa ruta (si es relativa, se ancla sola a la raíz del proyecto -- pero conviene una ruta absoluta para no depender del CWD del proceso), (3) programar el reemplazo mensual del archivo. Recordar la atribución que exige la licencia CC BY 4.0 de DB-IP (link visible a db-ip.com donde se muestre el dato). Opcionalmente, `IP_API_KEY` (+ `IP_API_BASE_URL`/`GEOIP_IP_API_TIMEOUT_SECONDS`) habilita el fallback a ip-api.com Pro para las IPs que la base local no cubra. Sin ninguna de las dos fuentes configuradas, `country`/`city` seguirán viniendo en `null` siempre -- no rompe nada, pero tampoco muestra ubicación hasta que se complete el despliegue.
- **Formato del QR/código de la TV**: sigue sin estandarizar en ningún documento del backend -- lo define el equipo de `appVideo`; confirmar antes de programar el parseo en mobile.
- **Posible incidente de pérdida de datos** (`docs/limpiar tablas.txt` + `restaurar_tablas.py`): cerrado -- las bases de datos se restauraron después de varias migraciones, no requiere auditoría adicional.
