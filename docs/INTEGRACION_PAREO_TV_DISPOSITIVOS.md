> ⚠️ **Documento superado.** Ver `docs/GUIA_INTEGRACION_UNIFICADA.md` (2026-07-28) -- consolida este documento y `GUIA_INTEGRACION_APPS.md` en una sola fuente, organizada por plataforma. Se conserva este archivo solo como referencia histórica.

# Integración móvil (iOS/Android): pareo de TV, dispositivos vinculados y gestión de cuenta

Fecha: 2026-07-27
Referencia: `docs/AUDITORIA_DECISIONES_Y_PENDIENTES.md`, secciones 27-30 (Fases 1-4, backend ya implementado y probado) y secciones 22/23/26 (password/cuenta).
Backend: Django + DRF + Channels (WebSocket sobre ASGI/Daphne). Todo lo descrito aquí ya existe y funciona en el backend — lo que falta es que las apps de iOS y Android lo consuman. La integración del lado de `appVideo` (Smart TV) para estos mismos flujos también sigue pendiente y se coordina por separado.

Este documento cubre 4 cosas, en el orden en que normalmente se usan:

1. Pareo de una Smart TV desde el celular (login social autoriza la TV).
2. Alternativa de pareo manual (sin login social), por si se necesita.
3. "Dispositivos vinculados": registrar el propio celular como dispositivo de la cuenta y poder verlos/revocarlos desde un panel tipo WhatsApp.
4. Cambiar contraseña, recuperar contraseña olvidada, y eliminar/cerrar la cuenta — y qué le pasa a los demás dispositivos vinculados cuando se hace cualquiera de esas dos primeras acciones.

Todos los endpoints son HTTPS/WSS sobre el mismo host que ya usa la app para el resto de la API (`https://backend.wind.do` en producción). No hace falta ningún SDK nuevo: todo es REST (JSON) + 2 conexiones WebSocket puntuales.

---

## 0. Conceptos previos

- **JWT de sesión**: se obtiene con login manual (`POST /api/auth/login/`) o login social (`POST /wind/auth/google/` o `/wind/auth/facebook/`). Es el mismo JWT que ya usa el resto de la app para `/api/v1/profile/...`. Los flujos de "dispositivos vinculados" (punto 3) lo necesitan.
- **`udid` + `temp_token`**: son la pareja de credenciales de un pareo de TV en curso. El `udid` (8 caracteres hex) identifica el intento de pareo; el `temp_token` es el secreto real (sin él, conocer el `udid` no sirve para nada). Los genera la TV al arrancar el flujo de pareo, y deben viajar SIEMPRE juntos en cada paso siguiente. Expiran a los 5 minutos si no se completa el pareo.
- **`device_token`**: secreto de 32+ bytes que identifica al propio celular como "dispositivo vinculado" de la cuenta (independiente del pareo de TV). Lo entrega el backend la primera vez que el celular se registra en `/ws/device/` y el celular debe guardarlo (Keychain / EncryptedSharedPreferences) para reconectarse como el mismo dispositivo en el futuro, no crear uno nuevo cada vez.

### 0.1 Login manual = DOS logins con las mismas credenciales, contra dos sistemas distintos

Esto es importante y no es obvio: cuando el usuario teclea `login1`/`login2`/código + password en la app (login manual, sin pasar por Google/Facebook), la app tiene que disparar **dos** llamadas de login con esas mismas credenciales, porque autentican contra dos sistemas que no se enteran entre sí:

1. **`clientLogin` directo contra PanAccess** — esto ya existe hoy en `appVideo` y no cambia con nada de este documento. Es la sesión que la app necesita para todo lo que es contenido real (canales, EPG, streaming, licencias). PanAccess no sabe nada del backend de Wind.
2. **`POST /api/auth/login/` contra el backend de Wind** — esto es lo nuevo. Devuelve el JWT que la app necesita para todo lo descrito en este documento (registrarse como dispositivo vinculado, cambiar contraseña vía Wind, cerrar cuenta, etc.). Wind no sirve canales ni streaming, solo gestiona la cuenta/suscriptor.

Para el usuario es invisible (teclea una sola vez), pero el equipo que lo programe tiene que decidir: (a) qué hacer si una de las dos llamadas falla y la otra no (¿deja pasar al usuario con funcionalidad reducida, o bloquea el login completo?), y (b) recordar que **después de cambiar la contraseña hay que refrescar las dos sesiones**, no solo una — el próximo `clientLogin` a PanAccess ya tiene que usar la contraseña nueva, y el JWT de Wind quedó invalidado por el cambio (ver sección 4.1), así que hay que volver a llamar a `/api/auth/login/` y volver a registrar el dispositivo en `/ws/device/`.

---

## 1. Pareo de TV vía login social (flujo principal)

Decisión de negocio: el celular **nunca recibe el password real de PanAccess** en este flujo. Solo confirma "sí, autorizo esta TV"; el password viaja cifrado directamente del backend a la TV.

Secuencia:

1. La TV muestra un código/QR que contiene un `udid` y un `temp_token` (los generó ella misma llamando a `GET /wind/request-udid-manual/`). **El formato exacto de ese QR/código lo define el equipo de TV** — la app solo necesita poder extraer de ahí los dos valores `udid` y `temp_token` (por QR, o por que el usuario los escriba a mano como fallback). Confirmar con el equipo de `appVideo` el formato final antes de programar el parseo.
2. El usuario escanea ese QR con el celular y hace login social normal (Google o Facebook) **agregando `udid` y `temp_token` al mismo body del login social**:

   ```
   POST /wind/auth/google/
   Content-Type: application/json

   {
     "access_token": "<token de Google Identity Services>",
     "udid": "a1b2c3d4",
     "temp_token": "<temp_token leído del QR>"
   }
   ```

   (Para Facebook es `POST /wind/auth/facebook/` con el mismo body, cambiando `access_token` por el access token de Facebook.)

3. Respuesta (200 OK) — es la misma respuesta de login social de siempre (JWT `access`/`refresh`), pero con dos diferencias cuando se manda `udid`+`temp_token`:
   - `panaccess_credentials` viene **siempre `null`** (a propósito: el celular no debe recibir el password real).
   - Se agrega un campo nuevo `udid_pairing`:

     ```json
     {
       "access": "<jwt>",
       "refresh": "<jwt>",
       "panaccess_credentials": null,
       "udid_pairing": { "ok": true, "udid": "a1b2c3d4", "subscriber_code": "BG$12345" }
     }
     ```

   - Si algo falla, `udid_pairing.ok` es `false` con un `code` para distinguir el motivo: `missing_params`, `invalid_udid`, `invalid_temp_token` (QR vencido/reusado), `expired`, `not_pending` (ya se autorizó o revocó antes), `rate_limited`, `subscriber_not_found` (si el backend tiene activo `SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER` y el correo no es abonado), `subscriber_unresolved` o `internal_error`. La app debe mostrarle al usuario un mensaje claro y, en la mayoría de estos casos, decirle que pida un código/QR nuevo a la TV (son de un solo uso, 5 minutos de vida).
   - **Importante:** si la petición se manda **sin** `udid`/`temp_token`, el login social funciona exactamente igual que hoy (con `panaccess_credentials` poblado) — este cambio es 100% aditivo y no rompe el login social normal que la app ya tiene.

4. La TV, mientras tanto, está esperando por WebSocket (`wss://.../ws/auth/`) o haciendo polling — en cuanto el paso 2-3 se completa, recibe sus credenciales cifradas automáticamente. **La app no tiene que hacer nada más en este flujo** una vez que `udid_pairing.ok=true` — puede mostrar "TV vinculada" y ya.

No hace falta que la app abra ningún WebSocket para este flujo; el único WebSocket relevante para el celular es el de dispositivos vinculados (punto 3).

---

## 2. Pareo manual (alternativa sin login social)

Existe una segunda forma de asociar un `udid` a un suscriptor, pensada originalmente para un operador/soporte o para cuando el usuario prefiere teclear datos en vez de usar login social. Si el producto lo requiere como fallback en la app, así es:

```
POST /wind/validate-and-associate-udid/
Content-Type: application/json

{
  "udid": "a1b2c3d4",
  "temp_token": "<temp_token del QR/código de la TV>",
  "subscriber_code": "BG$12345",
  "sn": "4001823830",
  "operator_id": "app-ios",
  "method": "manual"
}
```

- `sn` es el número de serie de una smartcard del suscriptor (tiene que existir y pertenecer a ese `subscriber_code`; si el usuario no lo sabe de memoria, la app puede traerlo de `GET /api/v1/profile/products/`, ya autenticado).
- Respuesta 200 con `{ message, udid, subscriber_code, smartcard_sn, status, validated_at, ... }`.
- Errores esperables: 400 (`errors` del serializer: udid no encontrado, `temp_token` inválido, UDID expirado o no está `pending`, SN no encontrado, SN de otro suscriptor, cuenta bloqueada, SN ya asociado a otro UDID activo) y 429 (rate limit: 1 solicitud por minuto por `udid`, más un límite general por dispositivo).

A diferencia del flujo social, aquí sí hay que conocer el `sn` de una smartcard — úsese solo si el flujo social (punto 1) no aplica para el caso de uso.

---

## 3. Dispositivos vinculados (registrar el celular, listar y revocar)

Esto es independiente del pareo de TV: es el registro del **propio celular** como dispositivo de la cuenta, para que el usuario pueda ver "estos son los dispositivos con acceso a mi cuenta" (como WhatsApp Web) y revocar cualquiera a distancia.

### 3.1 Registrar/refrescar el dispositivo (WebSocket)

Después de **cualquier** login (manual o social, con o sin pareo de TV de por medio), conectar:

```
wss://backend.wind.do/ws/device/?token=<access_token JWT>
```

- El JWT se manda como query param `token` (no hay forma de mandar headers en el handshake WS desde algunas plataformas, por eso va en la URL). Si el JWT es inválido/expiró/pertenece a un usuario sin suscriptor, el servidor cierra la conexión con código `4001` o `4004` sin más explicación — en ese caso hay que refrescar el JWT (`POST /api/auth/token/refresh/`) y reconectar.
- Al conectar, mandar:

  ```json
  {
    "type": "register_device",
    "device_type": "iOS",
    "device_model": "iPhone 15 Pro",
    "device_token": "<el que guardaste la vez anterior, o vacío/omitido la primera vez>"
  }
  ```

  `device_type` debe ser exactamente `"iOS"` (iPhone) o `"android"` (Android) — son los valores que el backend ya usa en su catálogo (`AppCredentials.APP_TYPES`); cualquier otro string se guarda igual (es texto libre truncado a 50 caracteres) pero conviene usar estos dos para que el dashboard filtre bien.

- Respuesta:

  ```json
  { "type": "device_registered", "device_token": "xyz...", "is_new": true }
  ```

  **Guardar `device_token` de forma persistente y segura (Keychain en iOS / EncryptedSharedPreferences o Keystore en Android)**. En la próxima sesión (nuevo login, o el mismo login después de cerrar la app), mandar ese mismo `device_token` en `register_device` para refrescar la fila existente en vez de crear una nueva — el backend limita a 20 dispositivos **nuevos** por hora por cuenta (no limita refrescar uno ya existente).
- Si el `device_token` que mandaste ya no es válido (fue revocado, o pertenece a otra cuenta), el servidor responde `{"type":"error","code":"device_token_invalid",...}` y cierra la conexión — en ese caso, borrar el `device_token` guardado y volver a registrarse sin él (se crea uno nuevo).
- El servidor manda `{"type":"ping"}` cada 30s; conviene responder `{"type":"pong"}` (no es obligatorio para que funcione, pero ayuda a detectar cortes de red rápido).
- **Revocación en vivo**: si el usuario revoca este dispositivo desde otro lado (ver 3.3) mientras la conexión sigue abierta, el servidor manda:

  ```json
  { "type": "device_revoked", "reason": "revoked_by_subscriber" }
  ```

  y cierra la conexión. La app debe interpretarlo como un cierre de sesión forzado: borrar el JWT y el `device_token` guardados, y mandar al usuario a la pantalla de login.
- Esta conexión no necesita quedar abierta todo el tiempo que el usuario usa la app — basta con abrirla, mandar `register_device`, y se puede cerrar apenas se recibe la confirmación. Si se quiere reaccionar a revocaciones **en vivo** (sin esperar al próximo login), hay que mantenerla abierta en segundo plano; si no, el efecto de una revocación de todos modos se aplica en el siguiente intento de reconectar (el `device_token` ya no será válido).

### 3.2 Ver mis dispositivos vinculados

```
GET /wind/devices/
Authorization: Bearer <access_token>
```

```json
{
  "devices": [
    {
      "id": 14,
      "device_type": "iOS",
      "device_model": "iPhone 15 Pro",
      "first_seen_at": "2026-07-20T10:00:00Z",
      "last_seen_at": "2026-07-27T09:15:00Z",
      "client_ip": "190.x.x.x"
    }
  ]
}
```

Nota: el `device_token` **nunca** aparece en esta lista (solo lo conoce el propio dispositivo). Se identifica cada fila por `id`, no por token.

### 3.3 Revocar un dispositivo

```
POST /wind/devices/14/revoke/
Authorization: Bearer <access_token>
```

- `200 OK` con `{"ok": true, ...}` si se pudo revocar.
- `404` (`not_found`) si el `id` no existe o no es de este usuario (a propósito indistinguible de "no existe", para no filtrar información).
- `409` (`already_revoked`) si ya estaba revocado.
- Límite: 60 solicitudes/minuto (throttle general de este endpoint, no específico por dispositivo).
- Si el dispositivo revocado sigue conectado a su WebSocket en ese momento, recibe el push `device_revoked` al instante (ver 3.1). Si no está conectado, la revocación se aplica la próxima vez que intente reconectarse.

---

## 4. Contraseña y cuenta

### 4.1 Cambiar contraseña (usuario ya logueado)

```
POST /api/v1/profile/password/
Authorization: Bearer <access_token>
Content-Type: application/json

{ "code": "BG$12345", "newPass": "NuevaClave123" }
```

- `code` debe ser el `subscriber_code` del usuario autenticado (el backend lo verifica contra el JWT; si no coincide, 403).
- `200 OK` → `{"success": true, "message": "Contraseña actualizada"}`.
- `502` si PanAccess falló (`error_type: "PanAccessException"`), `400` si el body no valida.
- **Efecto colateral automático (ya implementado en backend, la app no tiene que hacer nada extra):** al cambiar la contraseña se invalidan todos los JWT ya emitidos (`access`/`refresh` viejos dejan de servir de inmediato) y se revocan todos los `DeviceSession` de la cuenta — es decir, cualquier OTRO dispositivo vinculado (y la propia sesión actual del celular, si sigue usando el JWT viejo) queda deslogueado. **Después de un cambio de contraseña exitoso, la propia app debe volver a loguearse (obtener un JWT nuevo) y volver a registrar el dispositivo en `/ws/device/`** — el JWT que tenía antes del cambio ya no sirve, aunque haya sido el mismo dispositivo el que hizo el cambio.

### 4.2 Contraseña olvidada (usuario deslogueado)

```
POST /api/auth/password/forgot/
Content-Type: application/json

{ "email": "usuario@correo.com", "recaptcha_token": "<token de reCAPTCHA>" }
```

- Siempre responde `200` con un mensaje genérico (no revela si el correo existe o no). El backend manda un correo con un link/token.

```
POST /api/auth/password/reset-confirm/
Content-Type: application/json

{ "token": "<token del correo>", "newPass": "NuevaClave123", "confirmPass": "NuevaClave123" }
```

- `200` si se aplicó, `400` si el token es inválido/expiró/ya se usó (`error_type` en `TokenExpired`/`TokenUsed`/`InvalidToken`), `502` si PanAccess falló.
- Mismo efecto colateral que 4.1: invalida JWT existentes y revoca todos los `DeviceSession` de esa cuenta (tiene sentido: si alguien está resolviendo un "olvidé mi contraseña" es justamente porque puede haber un dispositivo ajeno con acceso).

**Nota sobre reCAPTCHA:** tanto "olvidé mi contraseña" como "eliminar cuenta" (4.3) exigen `recaptcha_token`. Hay que integrar el SDK de reCAPTCHA correspondiente (v2/v3 según lo que ya use el resto del backend) en ambas pantallas — sin esto, el backend devuelve 400 (`RecaptchaFailed`) siempre. Confirmar con el equipo de backend qué site-key usar para móvil (puede requerir una key distinta a la del sitio web).

### 4.3 Eliminar / cerrar la cuenta

```
POST /api/v1/profile/account/close/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "code": "BG$12345",
  "confirm": "BG$12345",
  "reason": "usuario decidió cancelar",
  "recaptcha_token": "<token de reCAPTCHA>",
  "dry_run": false
}
```

- `code` y `confirm` deben ser idénticos (doble confirmación por diseño: pedirle al usuario que teclee su código para confirmar, no solo un botón "Sí/No").
- `dry_run: true` es útil para un botón de "simular" en pruebas: corre toda la lógica de validación sin desaprovisionar nada de verdad en PanAccess.
- `200 OK` con `{"success": true/false, ...}` — puede ser `success: true` con `already_closed: true` si ya estaba cerrada antes.
- Este endpoint puede tardar varios segundos (desaprovisiona al suscriptor en PanAccess: quita productos, smartcards, bloqueos, y borra al suscriptor) — mostrar un loading, no un timeout corto.
- **Mismo efecto colateral que 4.1/4.2, más fuerte:** revoca sesiones/JWT, revoca **todos** los dispositivos vinculados y **todos** los pareos de TV pendientes/activos de esa cuenta — incluso si el cierre en PanAccess terminó parcial (falló a mitad de camino), el corte de acceso local ocurre siempre. Tras un cierre exitoso, la app debe borrar cualquier JWT/`device_token` local y mandar al usuario a la pantalla de login/onboarding (la cuenta ya no existe).
- Si `FeatureConfig.CLOSE_SUBSCRIBER_DASHBOARD_ENABLED` está desactivado en el backend, este endpoint devuelve `403` siempre — confirmar con backend que está prendido en el entorno donde se prueba.

---

## 5. Tabla resumen de endpoints

| Acción | Método y ruta | Auth |
|---|---|---|
| Login manual | `POST /api/auth/login/` | — |
| Login social (Google) | `POST /wind/auth/google/` | — |
| Login social (Facebook) | `POST /wind/auth/facebook/` | — |
| Refrescar JWT | `POST /api/auth/token/refresh/` | refresh token |
| Generar UDID (lo hace la TV) | `GET /wind/request-udid-manual/` | — |
| Pareo manual TV | `POST /wind/validate-and-associate-udid/` | — |
| Estado de un pareo (polling) | `GET /wind/validate/?udid=...&temp_token=...` | — |
| Desvincular una TV de un UDID | `POST /wind/disassociate-udid/` | — |
| Registrar/refrescar este dispositivo | WS `wss://.../ws/device/?token=<jwt>` | JWT (query) |
| Listar mis dispositivos | `GET /wind/devices/` | JWT |
| Revocar un dispositivo | `POST /wind/devices/<id>/revoke/` | JWT |
| Cambiar contraseña | `POST /api/v1/profile/password/` | JWT |
| Olvidé mi contraseña | `POST /api/auth/password/forgot/` | — |
| Confirmar reset de contraseña | `POST /api/auth/password/reset-confirm/` | — |
| Eliminar/cerrar cuenta | `POST /api/v1/profile/account/close/` | JWT |
| Mi perfil / suscriptor | `GET /api/v1/profile/me/` | JWT |
| Mis productos/smartcards | `GET /api/v1/profile/products/` | JWT |

---

## 6. Manejo de errores y rate limiting (aplica a todo lo anterior)

- Los endpoints de pareo (`request-udid-manual`, `validate-and-associate-udid`, `authenticate-with-udid`, `validate`, `disassociate-udid`) devuelven `429` con `retry_after` (segundos) y a veces header `Retry-After` — respetar ese tiempo antes de reintentar, no hacer retry inmediato en loop.
- Ningún endpoint de este documento devuelve nunca el detalle crudo de una excepción interna (`str(e)`) — los mensajes de error son siempre genéricos por diseño; el detalle queda solo en los logs del servidor. Si algo falla de forma rara, lo mejor es reportar el `code`/`error_type` recibido, no intentar parsear texto libre.
- Todos los `temp_token` se comparan en tiempo constante en el backend — no hay nada que optimizar del lado de la app ahí, solo transportarlos tal cual (no recortarlos, no cambiarles mayúsculas/minúsculas).

---

## 7. Checklist de implementación (iOS y Android, mismo alcance en ambos)

1. Login manual y social ya deberían existir en la app — agregar el soporte opcional de `udid`+`temp_token` en el body del login social para el flujo de pareo de TV (punto 1), sin tocar el camino normal.
2. Pantalla de "vincular TV": escanear QR (definir formato con el equipo de TV), extraer `udid`+`temp_token`, y si el usuario ya está logueado, disparar el login social (o, si ya tiene sesión activa, ver con backend si conviene exponer un endpoint específico solo para "autorizar pareo" sin repetir todo el login — no existe hoy, es tema a discutir si hace falta).
3. Implementar el fallback de pareo manual (punto 2) si el producto lo requiere.
4. Después de cualquier login exitoso (incluido el de después de cambiar contraseña o resetearla), conectar a `/ws/device/`, registrar el dispositivo, y persistir el `device_token` de forma segura.
5. Pantalla de "dispositivos vinculados": listar (`GET /wind/devices/`), revocar (`POST /wind/devices/<id>/revoke/`), y marcar visualmente "este dispositivo" comparando contra el `device_token` local (el backend no lo hace por vos: comparar por `id` no es posible desde el listado porque no expone el token, así que para saber cuál de la lista es "este dispositivo" hay que guardar también el `id` que devolvió `register_device`... revisar con backend si conviene agregarlo a la respuesta de `device_registered`, hoy solo devuelve `device_token`/`is_new`).
6. Manejar `device_revoked` (WS) y el cierre de conexión con código `4001`/`4004` como "hay que reautenticar".
7. Pantallas de cambiar contraseña, olvidé mi contraseña (con reCAPTCHA) y eliminar cuenta (con doble confirmación y reCAPTCHA) — en los tres casos, después de una respuesta exitosa, borrar JWT/`device_token` locales según corresponda (ver puntos 4.1-4.3) y forzar login de nuevo.
8. Probar explícitamente el caso "cambio la contraseña desde el celular A mientras el celular B tiene sesión abierta" — B debe quedar desconectado de `/ws/device/` (o fallar su próximo `register_device`) y su JWT debe dejar de servir en cualquier request REST.

---

## 8. Pendiente de definir antes de programar (no depende de la app, depende de coordinación con backend/TV)

- Formato exacto del QR/código que muestra la TV (contenido, encoding) — hoy lo define el equipo de `appVideo`, no está estandarizado en este documento porque no es responsabilidad del backend de Wind.
- Si conviene que `device_registered` (WS) devuelva también el `id` interno del `DeviceSession` (hoy solo devuelve `device_token`), para que la app pueda marcar "este dispositivo" en la lista de `GET /wind/devices/` sin heurísticas.
- Site-key de reCAPTCHA a usar en las pantallas de "olvidé mi contraseña" y "eliminar cuenta" desde móvil.

---

## 9. Prompt para pasar a los desarrolladores de iOS y Android

Copiar y pegar el siguiente bloque (o adaptarlo) al ticket/tarea de cada equipo:

> **Tarea: integrar pareo de TV, dispositivos vinculados y gestión de cuenta con el backend de Wind**
>
> Contexto: el backend (Django) ya tiene implementados y probados 4 flujos nuevos que la app todavía no consume: (1) pareo de una Smart TV escaneando un QR y confirmando con login social, sin que el celular vea nunca el password real; (2) un panel de "dispositivos vinculados" (como WhatsApp Web) para ver y revocar accesos desde otros dispositivos; (3) las pantallas ya existentes de cambiar/recuperar contraseña y eliminar cuenta, más el efecto nuevo de que esas acciones ahora revocan automáticamente todas las demás sesiones/dispositivos de la cuenta; (4) el detalle técnico completo de request/response de cada endpoint (URLs exactas, JSON, WebSocket, códigos de error, rate limits) está en `docs/INTEGRACION_PAREO_TV_DISPOSITIVOS.md` en el repo del backend — es la fuente de verdad, no adivinar formatos.
>
> Qué necesito que hagan (mismo alcance para iOS y Android, ambos equipos deben coordinar para que el comportamiento sea idéntico en las dos plataformas):
>
> 1. Agregar los campos opcionales `udid`+`temp_token` al login social existente (Google/Facebook), solo cuando el usuario esté escaneando el QR de una TV — sin `udid`/`temp_token`, el login social debe seguir funcionando exactamente igual que hoy.
> 2. Construir la pantalla de "vincular TV": escanear el QR, mandar el login social con esos dos campos, mostrar éxito/error según el `udid_pairing.code` que devuelva el backend (ver la tabla de códigos en la sección 1 del documento).
> 3. Después de cualquier login (manual, social, o justo después de cambiar/resetear contraseña), abrir una conexión WebSocket a `/ws/device/` con el JWT como query param, mandar `register_device` con el tipo/modelo del dispositivo, y guardar el `device_token` que responde el servidor en el almacenamiento seguro de la plataforma (Keychain en iOS, Keystore/EncryptedSharedPreferences en Android) para reutilizarlo en la próxima sesión.
> 4. Construir la pantalla de "dispositivos vinculados": listar con `GET /wind/devices/`, revocar con `POST /wind/devices/<id>/revoke/`, y manejar el mensaje `device_revoked` del WebSocket (forzar logout local si llega).
> 5. Revisar las pantallas ya existentes de cambiar contraseña, "olvidé mi contraseña" y eliminar cuenta contra los endpoints documentados (`/api/v1/profile/password/`, `/api/auth/password/forgot/`, `/api/auth/password/reset-confirm/`, `/api/v1/profile/account/close/`) y asegurarse de que, tras cualquiera de las tres, la app borre el JWT y el `device_token` guardados y obligue a un login nuevo — el backend invalida esas credenciales del lado suyo automáticamente, pero la app tiene que dejar de usarlas también del lado suyo.
> 6. Casos de prueba obligatorios antes de dar por terminado: (a) pareo de TV completo de punta a punta con un usuario real; (b) dos dispositivos con sesión abierta en la misma cuenta, cambiar la contraseña desde uno y confirmar que el otro queda deslogueado (por WS si está conectado, o al primer request si no); (c) eliminar la cuenta y confirmar que ningún dispositivo ni pareo de TV sigue funcionando después.
>
> Cualquier duda sobre formatos exactos de request/response, o si algo en el documento no coincide con lo que devuelve el backend real, avisar antes de asumir o adivinar — varios detalles (como el formato del QR de la TV) todavía están pendientes de confirmar con el equipo correspondiente.
