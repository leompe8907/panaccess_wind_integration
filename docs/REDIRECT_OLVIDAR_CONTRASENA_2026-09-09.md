# Redirect final de "olvidé mi contraseña": volver a la app vs. quedarse en el backend

Fecha: 2026-09-09

## Qué se pidió

El 2026-09-08 se cambió el redirect final de `reset-password.html` para que, en vez de caer en `/wind/login/` (página de test del backend), lleve siempre al sitio real / la app (mismo redirector inteligente `go_windtv_view` que ya usaba el correo de "contraseña actualizada"). El cliente notó un caso que ese cambio no cubría: si el usuario **está en la página del backend** y ejecuta la acción de olvidar contraseña, al terminar debería **seguir en el backend** -- pero si **está en las aplicaciones**, al terminar debería **volver a la app**. El cambio del día anterior mandaba a los dos casos por igual a la app/windtv.

## Por qué hacía falta distinguir el origen

Investigando el flujo completo (backend + `appVideo`), "olvidar contraseña" tiene tres caminos reales para *pedir* el enlace, pero los tres terminan exactamente en la misma página del backend (`reset-password.html`) para *poner la contraseña nueva*, porque ese paso siempre se hace desde el link que llega por correo:

- **App nativa (móvil, no TV, con `login.deviceSession` habilitado)**: modal 100% dentro de la app -- llama directo a `/api/auth/password/forgot/`, nunca abre ninguna página del backend para pedir el enlace.
- **TV**: muestra un QR con la URL de `login.forgotPassword.url` (la página del backend) para que el usuario la complete desde su teléfono.
- **PC sin modal nativo**: reemplaza la pestaña actual (`window.location.assign`) con esa misma URL del backend.
- **Alguien que entra directo** a `backend.wind.do/wind/forgot-password/` sin pasar por la app (uso interno/testing, o cualquier acceso fuera de la app).

En los primeros tres casos, el pedido "vino de la app" aunque el usuario esté viendo la página del backend en su navegador -- ahí hay que volver a la app/windtv al terminar. En el cuarto, no hay ninguna app a la que volver -- hay que quedarse en el backend. El backend no tenía ninguna forma de distinguir esto: el mismo endpoint, la misma página, sin ninguna señal de origen.

## Qué se hizo

Un parámetro `origin=app` opcional, **sin firmar** (no protege nada sensible -- en el peor caso alguien lo fuerza y solo cambia a qué pantalla termina, no hay ninguna acción de por medio), que viaja desde el pedido hasta el link del correo:

1. **App nativa**: `accountSecurityService.js::requestPasswordReset()` manda `origin: "app"` en el POST a `/api/auth/password/forgot/`.
2. **TV/PC**: `LoginPage.jsx` le agrega `?origin=app` a `forgotPasswordUrl` antes de armar el QR o navegar (`forgotPasswordUrlWithOrigin`) -- inofensivo si esa URL apunta a un sistema de otra marca que no sea este backend, un query param que no reconoce se ignora sin más.
3. **Backend, página de pedido** (`forgot_password_view`): si la URL trae `?origin=app`, lo reenvía tal cual en el POST que hace `forgot-password.html` a la API (para el caso TV/PC).
4. **Backend, API de pedido** (`password_forgot_view`): allowlist de un solo valor -- solo `"app"` pasa, cualquier otra cosa se descarta -- y se lo pasa a `request_password_reset(..., origin=...)`.
5. **`request_password_reset()`**: si `origin == "app"`, agrega `&origin=app` al link firmado que va en el correo.
6. **Backend, página de confirmación** (`reset_password_view`): lee `?origin=app` de la URL del link (si vino) y se lo pasa al template.
7. **`reset-password.html`**: al terminar de poner la contraseña nueva, si `ORIGIN === "app"` redirige a `go_windtv` (Android intenta abrir la app instalada, resto va a `windtv.wind.do`); si no, vuelve al comportamiento de siempre (`{% url 'login' %}`, se queda en el backend).

## Archivos tocados

- `wind/services/password_reset.py`: `request_password_reset()` gana el parámetro `origin` (keyword-only, default `""`).
- `wind/api/password_reset/views.py`: `password_forgot_view` sanitiza `origin` (allowlist de `"app"`) y lo pasa.
- `wind/views.py`: `forgot_password_view` y `reset_password_view` leen `?origin=app` de la URL y lo pasan al template.
- `wind/templates/wind/forgot-password.html`: reenvía `origin` en su POST si vino en la URL.
- `wind/templates/wind/reset-password.html`: decide el redirect final según `origin`.
- `appVideo/src/services/accountSecurityService.js`: `requestPasswordReset()` manda `origin: "app"`.
- `appVideo/src/pages/LoginPage.jsx`: `forgotPasswordUrlWithOrigin` (QR de TV y redirect de PC).
- `wind/tests/test_password_reset.py`: tests nuevos (ver abajo).

## Cómo se verificó

`py_compile` sobre los archivos de Python tocados. Tests nuevos en `wind/tests/test_password_reset.py`: que `request_password_reset()` agrega `origin=app` al link solo cuando se pide, que lo omite por defecto, que un valor que no sea `"app"` se descarta igual que si no viniera nada, que la API (`password_forgot_view`) sanitiza cualquier valor que no sea `"app"` (incluyendo un intento de inyección), y que las dos páginas (`forgot_password`/`reset_password`) reenvían `origin=app` a su template o quedan en `""` por defecto. 24/24 OK contra Postgres real (`pgserver`), junto con los 2 tests existentes de `test_go_windtv_view.py` para confirmar que no se rompió nada de ese redirector. No se pudo correr la suite completa de `wind` en este sandbox (mismo límite de siempre) -- recomendado correr `deploy\run_tests_local.bat` para la confirmación final.

El lado de `appVideo` no tiene suite de tests para este flujo específico en este repo; los cambios ahí son mínimos (una key más en un JSON body, una URL con un query param extra) y se revisaron a mano.
