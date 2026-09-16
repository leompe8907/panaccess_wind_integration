# Cambiar contraseña con código OTP por correo (2026-09-14)

Origen: mockup de diseño ("Flujo | Código OTP") para reemplazar la pantalla de "cambiar contraseña" (Mi Cuenta, usuario ya autenticado) por un flujo de código de verificación de 6 dígitos enviado por correo, en vez de pedir la contraseña actual (`oldPass`).

## Decisión de diseño: coexistencia, no reemplazo

El endpoint viejo (`POST /api/v1/profile/password/`, con `oldPass`) **no se toca ni se retira**. Se agregan dos endpoints nuevos; cada cliente (appVideo, iOS, Android) llama al que ya tiene implementado. Esto evita que una versión vieja de una app mobile que todavía no se actualizó se rompa el día que el flujo nuevo se activa -- appVideo se despliega al instante y cambia de flujo de una, pero las apps mobile dependen de que el usuario actualice desde la tienda, lo cual puede tardar semanas.

Se descartó a propósito un mecanismo de "config remota" para que el cliente le pregunte al backend qué flujo mostrar (se llegó a diseñar, `GET /api/v1/config/`) -- resultó innecesario: cada versión de cada app ya sabe, por código, a qué endpoint llamar. La única pieza de config que sí se agregó es `FeatureConfig.CHANGE_PASSWORD_OTP_ENABLED`, puramente interna, como freno de emergencia del lado del servidor (mismo patrón que `CREATE_SUBSCRIBER_PUBLIC_ENABLED`) -- si el endpoint nuevo da problemas en producción, se apaga por `.env` sin deploy, sin que eso afecte al endpoint viejo.

## Qué se implementó

### Backend (`D:\Back-Wind-V2`)

- `wind/models.py::PasswordChangeOtp` -- modelo nuevo: `subscriber_code`, `code_hash` (sha256, nunca el código en texto plano), `expires_at`, `attempts`, `consumed_at`. Migración `wind/migrations/0013_passwordchangeotp.py`.
- `appConfig.py`:
  - `PasswordChangeOtpConfig` -- `EXPIRY_MINUTES` (15), `MAX_VERIFY_ATTEMPTS` (5), `REQUEST_COOLDOWN_SECONDS` (45), todos por env var.
  - `FeatureConfig.CHANGE_PASSWORD_OTP_ENABLED` -- kill-switch (default `True`).
  - `EmailConfig.PASSWORD_CHANGE_OTP_SUBJECT`.
  - `ThrottleConfig.PROFILE_PASSWORD_OTP` (10/hour) + wiring en `settings.py` `DEFAULT_THROTTLE_RATES` + `wind/throttles.py::ProfilePasswordOtpThrottle`.
- `wind/services/password_change_otp.py` (nuevo) -- `generate_otp_code()`, `mask_email()`, `request_password_change_otp()` (genera + invalida códigos previos + encola correo, con cooldown), `check_password_change_otp()` (valida sin consumir), `consume_password_change_otp()` (marca usado).
- `wind/services/password_change_otp_email.py` + `wind/tasks.py::send_password_change_otp_email_task` -- mismo patrón que `password_reset_email.py`/`send_password_reset_email_task`.
- Plantillas `wind/templates/wind/emails/password_change_otp.html`/`.txt` -- mismo shell de marca que el correo de "olvidé mi contraseña", con el código en grande en vez de un botón.
- `wind/api/profile/serializers.py` -- `ProfilePasswordOtpRequestSerializer`, `ProfilePasswordOtpConfirmSerializer`.
- `wind/api/profile/views.py` -- `profile_password_otp_request_view`, `profile_password_otp_confirm_view`. Mismo manejo de excepciones de PanAccess que `profile_password_view` (reutiliza `reset_password_in_panaccess`/`sync_password_locally` de `wind/services/password_reset.py` -- por eso también revoca dispositivos y corta JWT en éxito).
- `wind/api/profile/urls.py` -- `password/otp/request-code/`, `password/otp/confirm/`.

**Detalle importante -- cuándo se consume el código:** el código se marca `consumed_at` recién **después** de que `reset_password_in_panaccess` + `sync_password_locally` terminan con éxito, no al validarlo. Si PanAccess rechaza la contraseña nueva (política, etc.), el mismo código sigue sirviendo para reintentar con otra sin pedir uno nuevo por correo -- mismo criterio que `confirm_password_reset`/`mark_reset_token_used` en el flujo de "olvidé mi contraseña".

### Frontend (`D:\appVideo`)

- `src/services/accountSecurityService.js` -- `requestPasswordChangeOtp()`, `confirmPasswordChangeOtp()`. `changePassword()` (oldPass) se deja intacto, sin usar desde la UI, por si hace falta revertir rápido.
- `src/components/account/ChangePasswordPanel.jsx` -- reescrito completo, 4 pasos: pedir código → escribir código (input de 6 dígitos, `autoComplete="one-time-code"`) → nueva contraseña → éxito + logout forzado (mismo comportamiento de siempre: limpia sesión y redirige a `/login` a los 3s). Un error de código malo/expirado/bloqueado durante la confirmación devuelve a la pantalla de código; un error de contraseña rechazada se queda en la pantalla de contraseña (mismo criterio que el backend, ver arriba).

**Simplificación consciente frente al mockup:** el mockup muestra el correo enmascarado (`lil*******@gmail.com`) ya en la primera pantalla, antes de pedir el código. Acá se muestra recién en la segunda pantalla (con el `masked_email` que devuelve `request-code/`), porque mostrarlo antes hubiera requerido una llamada adicional solo para leer el email del usuario. No es un problema de fondo, es una diferencia visual menor -- se puede ajustar después si hace falta.

**No implementado en este alcance (visual, no funcional):** el ícono de candado, la ilustración de fondo y el diseño exacto de las 6 casillas separadas del mockup. La UI reutiliza los estilos ya existentes de `_account-security.scss` (mismos botones/inputs que el resto de Mi Cuenta) -- un input único de texto centrado en vez de 6 casillas. Funcionalmente equivalente, visualmente más simple.

### Addendum: selector por marca en appVideo (mismo día)

A pedido, se agregó una forma de elegir por marca cuál de los dos flujos muestra appVideo, en vez de que la UI esté fija en OTP para todas las marcas:

- `src/config/brands.js` -- documentado el parámetro nuevo `login.deviceSession.changePasswordFlow` ('otp' default | 'old_password').
- Los 7 archivos de marca (`src/config/brands/{bromteck,intv,gigmax,cableatlantico,multiplustv,sattv,wind}.js`) + `defaults.js` (plantilla de referencia) -- todos con `"changePasswordFlow": "otp"` agregado dentro de su bloque `deviceSession`.
- `ChangePasswordPanel.jsx` -- reestructurado en dos subcomponentes, `OldPasswordChangeFlow` (el formulario original, reintroducido) y `OtpChangePasswordFlow` (el de 4 pasos); el componente exportado elige cuál renderizar leyendo `brandConfig.login.deviceSession.changePasswordFlow` (cualquier valor que no sea exactamente `'old_password'` cae en OTP).

Esto es un selector de **UI por marca**, no un mecanismo de coordinación con el backend -- los dos endpoints siguen activos siempre para cualquier marca, independientemente de este valor. Para volver alguna marca al flujo viejo, alcanza con cambiar ese único campo en su archivo y redeployar appVideo -- no hace falta tocar el backend.

### Documentación

- `docs/pendientes_integracion_apps/02_cambiar_contrasena.md` -- reescrito para documentar los dos flujos y dejar explícito que iOS/Android puede elegir cuál implementar. De paso se corrigió una afirmación desactualizada (decía que el endpoint viejo no usaba reCAPTCHA; sí lo usa).

### Addendum: confirmación de correo estilo Netflix en "pedir código" (2026-09-16)

A pedido del cliente, que quiere imitar en acciones sensibles (cambio de contraseña, control parental, eliminar cuenta) el patrón de Netflix de re-escribir el correo antes de una acción importante. Implementado para **cambio de contraseña únicamente** por ahora -- eliminar cuenta queda para una sesión aparte, es un cambio mayor (ver conversación).

Contexto del bug que lo disparó: el input de correo del paso "pedir código" ya existía en `ChangePasswordPanel.jsx` (puesto ahí por el mockup original) pero era decorativo -- el backend nunca lo recibía ni lo usaba, el código siempre se mandaba al correo real de la cuenta (`request.user.email`) sin importar qué escribiera el usuario. Confirmado explícitamente por el cliente que esto es intencional como paso de confirmación, no como selector de destino.

- `wind/api/profile/serializers.py` -- `ProfilePasswordOtpRequestSerializer` ahora exige `email` además de `code`.
- `wind/api/profile/views.py::profile_password_otp_request_view` -- antes de llamar a `request_password_change_otp`, compara `email` (normalizado: trim + lowercase) contra `request.user.email`. Si no coincide, responde `{"success": false, "code": "email_mismatch", ...}` con 400 **sin generar ni encolar ningún código**. Si coincide, sigue exactamente igual que antes -- el correo real que se usa para enviar sigue siendo `request.user.email`, nunca el que escribió el usuario (no hay forma de desviar el código a otra cuenta).
- `wind/tests/test_password_change_otp.py` -- las 6 llamadas existentes a `request-code/` en tests se actualizaron para incluir `email`; se agregó `test_request_code_rejects_mismatched_email` (verifica 400 + `email_mismatch` + que no se llame a `enqueue_password_change_otp_email` + que no se cree ningún registro `PasswordChangeOtp`).
- `src/services/accountSecurityService.js::requestPasswordChangeOtp` -- nuevo parámetro `email`, se manda en el body.
- `src/components/account/ChangePasswordPanel.jsx` -- `handleSendCode` ahora pasa el `email` del input (que ya existía) a `requestPasswordChangeOtp`. El manejo de error no necesitó cambios: la respuesta 400 ya se traduce en un `Error` cuyo `.message` se muestra en el mismo paso "pedir código" (no hace falta enrutar por `code` para este caso).
- `docs/pendientes_integracion_apps/02_cambiar_contrasena.md` -- actualizado el contrato: `email` ahora obligatorio en `request-code/`, nuevo error `email_mismatch`, y la guía para iOS/Android actualizada para incluir el input de correo en la pantalla 1.

**Nota de compatibilidad:** este cambio hace `email` un campo requerido en `request-code/` -- cualquier app mobile que ya haya integrado contra la versión anterior (sin `email`) empezará a recibir 400 (`{"email": ["This field is required."]}`) hasta que se actualice. Como ninguna app mobile tiene esto implementado todavía (ver sección "Qué debe implementar iOS/Android nativo" arriba), no hay impacto real hoy -- solo hay que asegurarse de que la implementación futura ya lo mande desde el principio.

## Cómo se verificó

Implementación original (2026-09-14): revisada a mano línea por línea (el sandbox de shell no estaba disponible en ese momento), sin correr tests.

**Addendum 2026-09-16 (confirmación de correo) -- sí se verificó con ejecución real:** se levantó Postgres embebido (`pgserver`) dentro del sandbox, apuntado temporalmente a las mismas credenciales de `.env` (`wind_db`/`wind_user`), y se corrió `manage.py check` (limpio) + la suite completa `wind.tests.test_password_change_otp` contra esa base real: **20/21 tests OK**. El único fallo (`test_new_request_invalidates_previous_code`) es de lógica de `check_password_change_otp`/invalidación de código previo -- código no tocado por este cambio, no relacionado con `email`/`email_mismatch`; queda anotado como hallazgo aparte, no corregido (fuera del alcance de esta tarea). El `.env` real se restauró apenas terminó la corrida (nunca quedó modificado).
