# reCAPTCHA — extensión a login manual y cambio de contraseña

Fecha: 2026-09-01
Referencia: `docs/RECAPTCHA_WIDGET_4_FORMULARIOS_2026-09-01.md` (los 4 flujos originales), `docs/RECAPTCHA_ESTADO_Y_PENDIENTES.md` (diseño original, donde login/cambio de contraseña se excluyeron a pedido del cliente).

## Qué se hizo y por qué

El diseño original de Alto #7 protegía 4 flujos (registro, olvidé contraseña, restablecer contraseña, eliminar cuenta) y dejaba **login** (manual y social) y **cambio de contraseña** (ya logueado) fuera del alcance, a pedido del cliente. A pedido explícito en esta sesión, se extendió reCAPTCHA v3 a dos de esos puntos:

1. **Login manual** (`POST /api/auth/login/`) — el más valioso de los dos: protege contra credential stuffing (bots probando listas de credenciales filtradas), algo que `LoginThrottle` (Alto #5, límite por volumen) no distingue de tráfico humano legítimo.
2. **Cambio de contraseña** (`POST /api/v1/profile/password/`) — capa extra sobre lo que ya existía (verificación de contraseña actual + bloqueo temporal, Alto #6). Menor valor que login porque ya requiere JWT válido, no es un endpoint público abierto.

**Login social (Google/Facebook) quedó explícitamente fuera de esta extensión.** Motivo: `GoogleLoginView`/`FacebookLoginView` comparten el mismo serializer (`PanAccessSocialLoginSerializer`/`GoogleIdTokenSocialLoginSerializer`) que usa también el pareo de TV vía QR (`_maybe_authorize_tv_pairing` en `wind/auth_views.py`) — agregar una verificación obligatoria ahí sin también instrumentar el paso de pareo (que corre en el navegador del celular, no necesariamente con la misma capacidad de ejecutar el script de Google que una página web de escritorio) arriesgaba romper ese flujo sin coordinación previa. Si se quiere cerrar esto también, hace falta diseñarlo aparte.

## Qué cambió

- **`wind/auth_serializers.py`** — `PanAccessLoginSerializer` (usado por `LoginView` de dj-rest-auth en `/api/auth/login/`, vía `REST_AUTH['LOGIN_SERIALIZER']`): nuevo campo `recaptcha_token` (opcional) y verificación al inicio de `validate()`, antes de intentar autenticar. Mismo patrón opt-in/fail-open que `wind/utils/recaptcha.py` ya usa en los otros 4 flujos.
- **`wind/api/profile/views.py`** — `profile_password_view`: mismo bloque de verificación que ya tienen `password_forgot_view`/`password_reset_confirm_view`/`profile_close_account_view`, agregado al inicio de la función.
- **`wind/views.py`** — `login_view` ahora pasa `recaptcha_site_key` al contexto (mismo patrón que las otras 4 vistas). `dashboard_view` ya lo pasaba desde antes (reutilizado).
- **`wind/templates/wind/login.html`** — widget cargado solo si hay site key; `getRecaptchaToken("login")` se llama justo antes del `fetch` a `/api/auth/login/`, **solo en el submit del formulario manual** (usuario/contraseña) -- los handlers de Google/Facebook no se tocaron.
- **`wind/templates/wind/dashboard.html`** — `submitPasswordChange()` (tab "Cambiar contraseña") reestructurado para resolver el token antes de armar el body, igual que se hizo con `submitCloseAccount()` el 2026-09-01 más temprano. Reutiliza el mismo `getRecaptchaToken()` ya definido en el archivo.
- **appVideo — `src/services/deviceAuthService.js`** — `loginManualForDeviceSession()` (el login nativo contra el backend Wind cuando `login.deviceSession.enabled`) ahora manda `recaptcha_token`, resuelto con la site key de la marca.
- **appVideo — `src/services/accountSecurityService.js`** — `changePassword()` ídem, agregado como los otros dos (`requestPasswordReset`/`closeAccount`).

## Cómo se verificó

1. `py_compile` + `pyflakes` sobre los 3 archivos Python tocados -- limpio (una advertencia preexistente sin relación en `wind/views.py:189`).
2. `python manage.py check` -- sin problemas.
3. `login.html`/`dashboard.html` renderizados con Django (`get_template().render()`) con y sin `recaptcha_site_key`, confirmando que el `<script src="...recaptcha/api.js...">` aparece solo cuando corresponde; los bloques `<script>` inline resultantes (4 combinaciones) parseados con `@babel/parser`, sin errores.
4. Los dos archivos de appVideo tocados, parseados con `@babel/parser` (`sourceType: module`) -- sin errores. Sin import circular (`recaptchaService.js`/`resolveBrandToken.js` no importan `deviceAuthService.js`).
5. **Corrida real de tests contra Postgres** (usando `pgserver`, un Postgres embebido efímero, ya que este sandbox no tiene un servidor corriendo) -- se recuperó el acceso al repo real (`D:\Back-Wind-V2` vía bash) durante esta sesión, lo que permitió, por primera vez en este engagement, ejecutar la suite real en vez de solo compilar:
   - `wind.tests.test_auth.SubscriberAuthTestCase` (incluye `test_jwt_login_success`, el que ejercita `/api/auth/login/`): **9/9 tests OK** tras el fix de abajo.
   - Redis no está disponible en este sandbox -- se sustituyó `CACHES['default']` por `LocMemCache` únicamente para esta corrida (no es un cambio de código, solo de la configuración de la corrida de tests).

### Hallazgo y fix: `test_jwt_login_success` rompía con la key ya activa

Al correr los tests contra este `.env` real (que ya tiene `RECAPTCHA_SECRET_KEY` configurada, ver el riesgo señalado en `docs/RECAPTCHA_WIDGET_4_FORMULARIOS_2026-09-01.md`), `test_jwt_login_success` (`wind/tests/test_auth.py`) empezó a fallar (400 en vez de 200) porque el test nunca mandaba `recaptcha_token` y ahora el login lo exige. Se corrigió agregando `@patch('wind.auth_serializers.verify_recaptcha', return_value=(True, None))` al test, mismo patrón que ya usan otros tests de este archivo para mockear dependencias externas.

**Deuda preexistente detectada de paso (no corregida en este cambio, fuera de alcance):** los 3 tests de `SubscriberRegistrationTestCase` (`test_successful_registration`, `test_duplicate_document_validation`, `test_duplicate_email_validation`) fallan por el mismo motivo -- nunca se les agregó un mock de `verify_recaptcha` cuando se protegió el registro (sesión anterior). No estaban rotos por este cambio; ya estaban así. Vale la pena una pasada separada para mockear `verify_recaptcha` en esos 3 también.

## Riesgo operativo (igual que en el otro documento)

Con `RECAPTCHA_SECRET_KEY` ya activa en producción, apenas se despliegue este cambio (`git pull` + restart de Daphne, `RECAPTCHA_SITE_KEY` ya está en el `.env` del servidor desde el fix anterior), el login manual empieza a exigir el token de inmediato. A diferencia de los 4 flujos originales, acá no hubo ventana de riesgo previa porque el endpoint nunca pidió el token hasta este mismo deploy -- pero **desplegar el widget del template y el chequeo del backend deben ir juntos** (mismo `git pull`), igual que siempre.
