# Olvidé mi contraseña

Estado: backend listo, incluida la bandera `origin=app` (2026-09-09). Implementado en appVideo solo para el camino "redirigir a la página del backend" (QR de TV, redirect de PC) -- el camino de "modal nativo dentro de la app" **todavía no lo implementa ningún cliente**, es el que le toca a iOS/Android si deciden no abrir una página web.
Referencia: `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 5.2, `docs/REDIRECT_OLVIDAR_CONTRASENA_2026-09-09.md`.

## Contrato

`POST /api/auth/password/forgot/` (`email`, `recaptcha_token`) → siempre 200 genérico (no revela si el email existe). `POST /api/auth/password/reset-confirm/` (`token`, `newPass`, `confirmPass`) → 200 si aplicó.

`newPass` sigue la misma política y tabla de errores que `02_cambiar_contrasena.md` (`password_policy_violation`, `password_rejected_by_panaccess`, más `TokenExpired`/`TokenUsed`/`InvalidToken` si el link ya no sirve). Mismo efecto colateral que cambiar contraseña (revoca todos los JWT/dispositivos) solo si el reset se completa con éxito.

## El punto importante: dónde termina el usuario después

El usuario siempre pone la contraseña nueva en una página web del backend (`/wind/reset-password/`), a la que llega por el link del correo -- eso no cambia nunca, no importa qué cliente inició el pedido. Lo que sí puede cambiar es **a dónde lo manda esa página al terminar**: por defecto se queda en el backend (piensa que es un usuario de PC); con `origin=app` redirige de vuelta a la app (`go_windtv` -- intent Android o `windtv.wind.do`).

Hay dos caminos completamente distintos para llegar a ese link, y cada uno manda la bandera de una forma distinta:

### Camino A -- la app pide el enlace ella misma (modal nativo, sin abrir ninguna página)

Agregar `"origin": "app"` al body del `POST /api/auth/password/forgot/`. El backend arma el link del correo con `&origin=app` ya incluido -- la app no tiene que hacer nada más después de mandar el correo, el usuario sigue el link desde su cliente de correo y la página se encarga sola.

### Camino B -- la app manda al usuario a la página del backend para pedir el enlace

Si en cambio la app abre `/wind/forgot-password/` (por ejemplo un QR que apunta ahí, o un redirect de pestaña completa) en vez de llamar directo a la API, hay que agregarle `?origin=app` a esa URL antes de navegar/generar el QR -- la página se encarga de reenviarlo sola desde ahí en adelante, incluyendo en el link del correo que termina generando.

Cualquier otro valor (o no mandar el campo) es el comportamiento de siempre -- no rompe nada.

## Estado en appVideo (web)

Implementado, camino B únicamente: `requestPasswordReset()` en `accountSecurityService.js` manda `origin: 'app'` en el body (esto en realidad es el camino A técnicamente, pero appVideo lo usa para el QR/redirect a la página, no para un modal 100% nativo), y `LoginPage.jsx` le agrega `?origin=app` a la URL del QR de TV y al redirect de PC (`forgotPasswordUrlWithOrigin`). No hay un modal "olvidé mi contraseña" 100% dentro de appVideo que nunca toque la página del backend.

## Qué debe implementar iOS/Android nativo

Depende de qué UX elija cada app:

- **Si la app tiene (o va a tener) un modal nativo de "olvidé mi contraseña" que llama directo a `POST /api/auth/password/forgot/`** sin abrir ningún navegador: mandar `"origin": "app"` en el body de ese POST (Camino A). El usuario va a terminar el flujo en el navegador del sistema igual (ahí es donde pone la contraseña nueva, sección "El punto importante" arriba) -- pero al terminar, esa página lo va a devolver a la app en vez de dejarlo en el backend.
- **Si en cambio la app prefiere redirigir a la página del backend para todo el flujo** (más simple de mantener, mismo comportamiento que appVideo hoy): agregar `?origin=app` a la URL de `/wind/forgot-password/` antes de abrirla (Camino B).
- En ambos casos, el reCAPTCHA de este endpoint tiene la misma limitación en apps nativas que el resto de acciones sensibles -- ver `08_recaptcha.md` antes de asumir que `recaptcha_token` siempre se puede generar.
- Tras un reset exitoso, mismo efecto colateral que cambiar contraseña: JWT y dispositivos revocados -- la app debe re-loguear y re-registrar el dispositivo cuando el usuario vuelva (ver `02_cambiar_contrasena.md` y `07_dispositivos_vinculados.md`).

## Nota de seguridad relacionada (no es parte de este endpoint)

La contraseña real del suscriptor viaja en texto plano en otros dos lugares del sistema (correo de bienvenida al registrarse, y la respuesta de login social) -- es una decisión de negocio ya aceptada, no un pendiente de "olvidé contraseña". Detalle en `05_login_social_tv.md`.
