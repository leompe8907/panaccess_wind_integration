# Cambiar contraseña

Estado: **dos flujos coexisten a propósito**, ninguno reemplaza al otro:

- **Flujo viejo (`oldPass`)** -- implementado desde 2026-08-28, estable. appVideo ya no lo usa desde 2026-09-14, pero el backend lo sigue aceptando siempre (no se retira).
- **Flujo nuevo (código OTP por correo)** -- agregado 2026-09-14, ver `docs/CAMBIO_CONTRASENA_OTP_2026-09-14.md`. Es el que usa appVideo hoy.

Cada app llama al que ya tiene implementado -- no hace falta que coincidan entre plataformas ni que se actualicen todas a la vez. Referencia general: `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 5.1 (todavía describe el flujo viejo; agregar referencia al nuevo si se actualiza esa guía).

## Flujo viejo: `oldPass`

`POST /api/v1/profile/password/` (JWT), body `{"code": "<subscriber_code>", "oldPass": "...", "newPass": "..."}` → `{"success": true, "message": "Contraseña actualizada"}`.

`oldPass` es obligatorio -- el backend la verifica contra PanAccess antes de aplicar el cambio (evita que un JWT robado alcance por sí solo para cambiar la contraseña).

**Errores** (siempre con `"success": false` y `"message"`, más `"code"` cuando aplica):

| Status | `code` | Significado |
|---|---|---|
| 400 | `old_password_incorrect` | `oldPass` no coincide con la real. |
| 400 | `password_policy_violation` | `newPass` no cumple la política (detectado localmente). |
| 400 | `password_rejected_by_panaccess` | PanAccess rechazó por una regla no cubierta localmente. Incluye `panaccess_error_code`. |
| 429 | `old_password_locked` | 5 intentos fallidos seguidos de `oldPass` -- bloqueado 15 min. |
| 429 | `rate_limited` | Demasiados intentos en general. |
| 502/503/504 | `panaccess_integration_error`/`panaccess_unavailable`/`panaccess_timeout` | Problema de PanAccess, no del valor enviado. |

Este endpoint sí manda `recaptcha_token` (reCAPTCHA v3, opcional/opt-in -- ver abajo).

## Flujo nuevo: código OTP por correo (2026-09-14)

Dos llamadas en vez de una. **Kill-switch de backend:** `FeatureConfig.CHANGE_PASSWORD_OTP_ENABLED` -- si está apagado, ambos endpoints devuelven 404 (solo afecta a este flujo, el de `oldPass` nunca se ve afectado).

### 1. Pedir el código

`POST /api/v1/profile/password/otp/request-code/` (JWT), body `{"code": "<subscriber_code>", "email": "<correo escrito por el usuario>"}` →

```json
{"success": true, "masked_email": "lil***@gmail.com", "expires_in_minutes": 15, "message": "Te enviamos un código de verificación a tu correo."}
```

**`email` es obligatorio desde 2026-09-16** (antes no existía este campo). No es el destino del código -- el código siempre se manda al correo real que ya tiene la cuenta (`request.user.email`), la app nunca puede redirigirlo a otro lado. Es un paso de **confirmación estilo Netflix** para acciones sensibles: el usuario re-escribe su propio correo antes de continuar, y el backend valida que coincida (case-insensitive, trim) con el de la cuenta autenticada. Si no coincide, no se genera ni se envía ningún código.

Manda un código de 6 dígitos al correo del usuario autenticado, válido 15 minutos, un solo uso. Pedir un código nuevo invalida cualquier código anterior sin usar.

Errores:

| Status | `code` | Significado |
|---|---|---|
| 400 | `email_mismatch` | El correo escrito no coincide con el de la cuenta -- no se envió nada. (2026-09-16) |
| 400 | `no_email` | La cuenta no tiene correo registrado. |
| 429 | `otp_cooldown` | Se pidió otro código hace muy poco (~45s por defecto) -- incluye `wait_seconds`. |
| 404 | -- | Flujo deshabilitado por `CHANGE_PASSWORD_OTP_ENABLED=false`. |

### 2. Confirmar código + nueva contraseña

`POST /api/v1/profile/password/otp/confirm/` (JWT), body `{"code": "<subscriber_code>", "otpCode": "123456", "newPass": "..."}` → `{"success": true, "message": "Contraseña actualizada correctamente. Se cerró sesión en todos tus dispositivos."}`

No hay un endpoint separado para "solo verificar el código sin cambiar la contraseña" -- ambas cosas se confirman juntas acá. Si la UI nativa muestra el código y la contraseña nueva en pantallas separadas (como el mockup de diseño), la app debe guardar el código localmente entre pantallas y mandarlo recién en esta llamada.

**Errores del código** (el usuario debe volver a la pantalla de "escribir código", no a la de contraseña):

| Status | `code` | Significado |
|---|---|---|
| 400 | `otp_incorrect` | Código incorrecto. Incluye `attempts_remaining`. |
| 400 | `otp_missing_or_expired` | Expiró (15 min), no se pidió ninguno, o ya se pidió uno más nuevo que lo invalidó. |
| 429 | `otp_locked` | 5 intentos fallidos con el código actual -- hay que pedir uno nuevo. |

**Errores de la contraseña** (el código sigue siendo válido, no hace falta pedir uno nuevo -- solo se consume tras un cambio exitoso):

| Status | `code` | Significado |
|---|---|---|
| 400 | `password_policy_violation` | `newPass` no cumple la política. |
| 400 | `password_rejected_by_panaccess` | PanAccess rechazó la contraseña. Incluye `panaccess_error_code`. |
| 502/503/504 | `panaccess_integration_error`/`panaccess_unavailable`/`panaccess_timeout` | Problema de PanAccess. |

Ambos endpoints mandan `recaptcha_token` igual que el resto de las acciones sensibles (ver abajo).

**Política de `newPass`:** misma de siempre -- 8-255 caracteres, al menos una mayúscula, al menos un número, charset `a-z A-Z 0-9 - _` más especiales `! @ # $ % ^ & * ( ) + = [ ] { } ; : ' " , . < > / ? ~ \``. Validar localmente antes de llamar para evitar el round-trip en el caso obvio.

## Efecto colateral automático (solo en éxito, cualquiera de los dos flujos)

Invalida **todos** los JWT ya emitidos y revoca **todos** los `DeviceSession` de la cuenta -- incluida la propia sesión que hizo el cambio. Esto dispara el mismo push `device_revoked` (razón `"password_changed"`) que una revocación manual -- ver `07_dispositivos_vinculados.md` para el manejo esperado de ese push.

## reCAPTCHA

Los tres endpoints (el viejo y los dos nuevos) mandan `recaptcha_token` opcionalmente -- opt-in de los dos lados: si la app no tiene site key configurada, no manda el campo; si el backend no tiene `RECAPTCHA_SECRET_KEY` configurado, no lo exige. (Corrección: una versión anterior de este documento decía que el endpoint viejo no usaba reCAPTCHA -- sí lo usa, ver `wind/api/profile/views.py::profile_password_view`.)

## Estado en appVideo (web)

Implementado con el **flujo nuevo (OTP)**: `requestPasswordChangeOtp()`/`confirmPasswordChangeOtp()` en `accountSecurityService.js`, UI de 4 pasos en `ChangePasswordPanel.jsx`. `changePassword()` (oldPass) se deja sin usar en el código, sin eliminar, por si hace falta un rollback rápido.

## Qué debe implementar iOS/Android nativo

Elegir uno de los dos flujos (no hace falta implementar los dos):

**Opción recomendada -- flujo OTP (igual que appVideo hoy):**
- Pantalla 1: input de correo (el usuario re-escribe el suyo, paso de confirmación estilo Netflix) + botón "Enviar código" → `POST .../otp/request-code/` con `email`. Si responde `email_mismatch`, mostrar el error ahí mismo sin avanzar. Si tiene éxito, mostrar `masked_email` de la respuesta.
- Pantalla 2: input de 6 dígitos → validar formato localmente, guardar en memoria, no llamar al backend todavía.
- Pantalla 3: nueva contraseña + confirmar → recién acá `POST .../otp/confirm/` con el código guardado + la contraseña. Si el error es `otp_incorrect`/`otp_missing_or_expired`/`otp_locked`, volver a la pantalla 2 mostrando el mensaje; cualquier otro error se queda en la pantalla 3.
- Pantalla 4: éxito + logout forzado (ver abajo).

**Opción alternativa -- seguir con `oldPass`** (si ya estaba implementado y no se quiere tocar): sigue funcionando exactamente igual que antes, sin fecha de retiro planeada.

En ambos casos, **tras un cambio exitoso la app queda con un JWT y un `device_token` inválidos** (el propio backend los revocó como efecto colateral). La app debe:
1. Recibir/reaccionar al push `device_revoked` (razón `password_changed`) en su conexión `ws/device/` abierta -- mismo manejo que cualquier revocación forzada (ver `07_dispositivos_vinculados.md`, punto "a").
2. Volver a loguearse con la contraseña nueva -- **ambas llamadas**, no solo una: PanAccess (contenido/streaming) y `/api/auth/login/` (JWT de este documento) -- ver `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 0 y `05_login_social_tv.md`.
3. Volver a abrir `ws/device/` y registrar el dispositivo de nuevo (el `device_token` viejo ya no sirve).
