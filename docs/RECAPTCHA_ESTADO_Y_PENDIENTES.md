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
  - El inicio de sesión y el cambio de contraseña desde el dashboard (ya logueado) quedaron fuera de este alcance a pedido del cliente.
- **Tipo:** reCAPTCHA **v3** — la versión invisible, sin checkbox "no soy un robot". Evalúa el comportamiento del usuario y devuelve un puntaje de 0 a 1; si es muy bajo, se rechaza el envío.
- **Comportamiento actual:** la verificación es "opt-in" — mientras no exista la llave secreta en el entorno, el sistema no bloquea absolutamente nada en ninguno de los 4 endpoints. Es decir, está construido pero apagado a propósito, para no romper nada mientras no esté todo listo.

## Qué falta para activarlo completamente

1. **Conseguir las llaves de Google** (ver instrucciones abajo).
2. **Configurar la llave secreta** en el entorno del servidor (`RECAPTCHA_SECRET_KEY`) — una sola llave sirve para los 4 flujos.
3. **Agregar el widget en cada formulario del frontend** (`register.html`, `forgot-password.html`, `reset-password.html`, y el modal de "Eliminar cuenta" del dashboard): cargar el script de Google y generar el token antes de enviar cada formulario. Este paso todavía no está hecho en ninguno de los 4 — es el que falta para que el backend reciba el token que ahora ya sabe validar en todos.

Si solo se configura la llave secreta sin este último paso, los 4 flujos empezarían a **fallar siempre** (el backend esperaría un token que el formulario nunca envía).

## Cómo obtener las llaves

1. Entrar a **https://www.google.com/recaptcha/admin/create** con una cuenta de Google (idealmente una cuenta de la empresa, no personal).
2. Ponerle una etiqueta al registro, por ejemplo "Wind — producción".
3. En "Tipo de reCAPTCHA", elegir **reCAPTCHA v3** (no v2 / checkbox — el código actual está pensado para v3).
4. Agregar los dominios donde va a funcionar (dominio de producción, y opcionalmente `localhost` para pruebas).
5. Aceptar los términos y enviar.

Google entrega dos llaves:

- **Site key** (pública): va en el frontend, en el widget del formulario de registro.
- **Secret key** (privada): va en el servidor, en la variable de entorno `RECAPTCHA_SECRET_KEY`.

## Recomendación

Completar los dos pasos pendientes (llaves + widget en los 4 formularios) antes de considerar estos flujos protegidos contra bots. Es un cambio acotado y de bajo riesgo, ya que la lógica de backend ya está probada y lista en los 4 endpoints — solo falta conectar las piezas del lado del frontend.
