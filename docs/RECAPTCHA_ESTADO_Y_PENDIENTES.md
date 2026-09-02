# reCAPTCHA en Wind — estado actual y pendientes

Documento de referencia para explicar al cliente el estado de la protección anti-bots (reCAPTCHA) en el portal.

## Resumen ejecutivo

reCAPTCHA está **parcialmente implementado**: la lógica de verificación en el backend ya existe y funciona para los 4 flujos públicos que la necesitan (registro, olvidé contraseña, restablecer contraseña y eliminar cuenta), pero está **desactivada** porque falta un dato de configuración (la llave secreta), y además falta agregar el widget correspondiente en cada formulario del frontend. Hoy, ninguno de estos 4 flujos tiene protección anti-bot activa todavía.

## ¿Qué es y para qué sirve?

reCAPTCHA es un servicio gratuito de Google que detecta si quien está llenando un formulario es una persona real o un script/bot automatizado. Se usa típicamente para evitar que alguien cree cuentas en masa, dispare recuperaciones de contraseña masivas, o automatice acciones sensibles como eliminar cuentas.

En Wind se identificaron 4 puntos de riesgo, todos endpoints accesibles sin necesidad de iniciar sesión (o, en el caso de eliminar cuenta, una acción sensible e irreversible):

1. Registro de nuevos suscriptores.
2. Olvidé mi contraseña (solicitud del enlace de recuperación).
3. Restablecer contraseña (confirmación con el enlace del correo).
4. Eliminar cuenta.

## Qué existe hoy en el código

- **Lógica de verificación (backend):** `wind/utils/recaptcha.py`. Envía el token recibido del formulario a los servidores de Google y valida la respuesta.
- **Configuración:** `appConfig.py`, clase `RecaptchaConfig`. Lee dos variables de entorno:
  - `RECAPTCHA_SECRET_KEY` — la llave secreta (no está configurada actualmente).
  - `RECAPTCHA_MIN_SCORE` — puntaje mínimo aceptado (por defecto 0.5).
- **Dónde se aplica** (los 4 endpoints ya están conectados a la verificación):
  - Registro — `wind/functions/create_subscriber.py` (`create_subscriber_view`).
  - Olvidé contraseña — `wind/api/password_reset/views.py` (`password_forgot_view`).
  - Restablecer contraseña — `wind/api/password_reset/views.py` (`password_reset_confirm_view`).
  - Eliminar cuenta — `wind/api/profile/views.py` (`profile_close_account_view`).
  - El inicio de sesión y el cambio de contraseña desde el dashboard (ya logueado) quedaron fuera de este alcance a pedido del cliente. **Actualización 2026-09-01:** se extendió a login manual y a cambio de contraseña -- ver `docs/RECAPTCHA_LOGIN_Y_CAMBIO_PASSWORD_2026-09-01.md`. Login social (Google/Facebook) sigue fuera, ver ese documento para el motivo.
- **Tipo:** reCAPTCHA **v3** — la versión invisible, sin checkbox "no soy un robot". Evalúa el comportamiento del usuario y devuelve un puntaje de 0 a 1; si es muy bajo, se rechaza el envío.
- **Comportamiento actual:** la verificación es "opt-in" — mientras no exista la llave secreta en el entorno, el sistema no bloquea absolutamente nada en ninguno de los 4 endpoints. Es decir, está construido pero apagado a propósito, para no romper nada mientras no esté todo listo.

## Qué falta para activarlo completamente

1. **Conseguir las llaves de Google** (ver instrucciones abajo).
2. **Configurar la llave secreta** en el entorno del servidor (`RECAPTCHA_SECRET_KEY`) — una sola llave sirve para los 4 flujos.
3. **Agregar el widget en cada formulario del frontend** (`register.html`, `forgot-password.html`, `reset-password.html`, y el modal de "Eliminar cuenta" del dashboard): cargar el script de Google y generar el token antes de enviar cada formulario. Este paso todavía no está hecho en ninguno de los 4 — es el que falta para que el backend reciba el token que ahora ya sabe validar en todos.

Si solo se configura la llave secreta sin este último paso, los 4 flujos empezarían a **fallar siempre** (el backend esperaría un token que el formulario nunca envía).

## Cómo obtener las llaves (actualizado 2026-08-26 -- ver nota importante)

**Nota importante:** Google migró todo reCAPTCHA a "Google Cloud Fraud Defense" (antes llamado "reCAPTCHA Enterprise"). Ya no se puede crear una llave "Classic" suelta como antes -- desde Q3 2024 no se permiten llaves Classic nuevas, y desde Q1 2026 Google terminó de migrar automáticamente hasta las llaves Classic viejas que quedaban sin proyecto de Google Cloud asociado. **Esto no rompe el plan original ni el código que ya existe** (`wind/utils/recaptcha.py` sigue funcionando igual, sin cambios), pero el proceso para conseguir la llave tiene un par de pasos nuevos respecto a lo que decía este documento antes. Ver la guía completa y verificada en `docs/GUIA_CREAR_LLAVES_RECAPTCHA_2026-08-26.md`.

Resumen rápido:

1. Entrar a **https://www.google.com/recaptcha/admin/create** con una cuenta de Google (idealmente una cuenta de la empresa, no personal).
2. Ponerle una etiqueta al registro, por ejemplo "Wind — producción".
3. En "Tipo de reCAPTCHA", elegir **Score based (v3)** (no v2 / checkbox — el código actual está pensado para v3).
4. Agregar los dominios donde va a funcionar (`backend.wind.do`, y opcionalmente `localhost` para pruebas).
5. Aceptar los términos y enviar. Google crea automáticamente, sin costo, un proyecto de Google Cloud detrás de escena para alojar la llave -- no hace falta configurar nada de Google Cloud a mano ni cargar una tarjeta (el nivel gratuito "Essentials" da 10,000 verificaciones/mes sin necesidad de facturación).
6. Google entrega la **Site key** (pública, va en el frontend) de inmediato.
7. **Paso nuevo que antes no hacía falta:** para conseguir la **Secret key** (la que va en `RECAPTCHA_SECRET_KEY`), hay que entrar a la consola de Google Cloud (el enlace que llega en el correo de confirmación), abrir el detalle de la llave, pestaña "Integration", y click en **"Use Legacy Key"** -- ahí aparece la secret key clásica, compatible 1:1 con lo que `wind/utils/recaptcha.py` ya sabe usar.

## Recomendación

Completar los dos pasos pendientes (llaves + widget en los 4 formularios) antes de considerar estos flujos protegidos contra bots. Es un cambio acotado y de bajo riesgo, ya que la lógica de backend ya está probada y lista en los 4 endpoints — solo falta conectar las piezas del lado del frontend.
