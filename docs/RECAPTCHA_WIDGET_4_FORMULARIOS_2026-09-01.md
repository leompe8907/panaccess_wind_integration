# reCAPTCHA — widget agregado en los 4 formularios (backend)

Fecha: 2026-09-01
Referencia: `docs/PLAN_ACTIVACION_RECAPTCHA_2026-08-26.md` (plan seguido paso a paso), `docs/RECAPTCHA_ESTADO_Y_PENDIENTES.md` (estado previo), `docs/GUIA_CREAR_LLAVES_RECAPTCHA_2026-08-26.md` (cómo se consiguieron las llaves).

## Qué se hizo

Se completó el paso 2 del plan: agregar el widget de reCAPTCHA v3 (invisible) en los 4 formularios server-side de Wind que ya estaban conectados a `verify_recaptcha()` del lado backend, pero que hasta ahora no mandaban ningún `recaptcha_token`.

1. **`appConfig.py`** — `RecaptchaConfig` ahora también lee `RECAPTCHA_SITE_KEY` (pública), además de `SECRET_KEY` y `MIN_SCORE` que ya existían.
2. **`.env`** — se descomentó `RECAPTCHA_SITE_KEY` (ya tenía el valor correcto cargado, solo estaba comentada).
3. **`wind/views.py`** — `register_view`, `forgot_password_view`, `reset_password_view` (rama de éxito) y `dashboard_view` ahora pasan `recaptcha_site_key` al contexto del template (mismo patrón que `google_client_id`/`facebook_app_id` en `login_view`).
4. **4 templates** — cada uno carga el script de Google (`<script src="https://www.google.com/recaptcha/api.js?render=...">`) solo si `recaptcha_site_key` está configurada, define `getRecaptchaToken(action)`, y lo llama justo antes del `fetch`/`WindAuth.fetchApi` correspondiente, agregando `recaptcha_token` al body solo si se obtuvo un token:
   - `register.html` → acción `"register"`, antes del POST a `/wind/create-subscriber/`.
   - `forgot-password.html` → acción `"forgot_password"`, antes del POST a `/api/auth/password/forgot/`.
   - `reset-password.html` → acción `"reset_password"`, antes del POST a `/api/auth/password/reset-confirm/`.
   - `dashboard.html` (modal "Eliminar cuenta") → acción `"close_account"`, antes del POST a `/api/v1/profile/account/close/` (se reestructuró `submitCloseAccount()` para resolver el token primero y recién ahí armar el body).

## Por qué (opt-in de los dos lados, fail-open)

`getRecaptchaToken()` es idéntica en los 4 templates: si `recaptcha_site_key` está vacía o `grecaptcha` no cargó, devuelve `Promise.resolve(null)` de inmediato y el formulario sigue funcionando exactamente igual que antes (sin bloquear nada). Si el token falla por cualquier motivo (red, timeout, etc.), también resuelve `null` en vez de propagar el error — nunca impide el envío del formulario. Es el mismo patrón ya usado en appVideo (`recaptchaService.js`), para que el comportamiento sea consistente en todas las superficies.

Del lado backend, `verify_recaptcha()` (`wind/utils/recaptcha.py`) solo exige el token si `RECAPTCHA_SECRET_KEY` está configurada — así que mientras no se complete la activación, tener o no `recaptcha_token` en el body no cambia nada.

## ⚠️ Riesgo detectado durante esta implementación

`RECAPTCHA_SECRET_KEY` **ya estaba configurada y activa en `.env`** antes de este cambio (con el mismo valor que el cliente compartió), mientras que `RECAPTCHA_SITE_KEY` estaba comentada. Esto es el orden **inverso** al recomendado en `docs/PLAN_ACTIVACION_RECAPTCHA_2026-08-26.md` sección 4 — significa que, si el backend llegó a correr con esa `SECRET_KEY` activa en algún momento (por ejemplo, tras el restart de Daphne hecho más temprano hoy para el fix de `applogs`), **los 4 flujos (registro, olvidé contraseña, restablecer contraseña, eliminar cuenta) pudieron haber estado rechazando el 100% de los intentos reales** durante esa ventana, porque ningún formulario mandaba token todavía.

Este cambio (agregar el widget) es lo que corrige esa situación — a partir de este deploy, los 4 formularios sí mandan `recaptcha_token`. Pero **antes de dar esto por resuelto en producción**, conviene:

1. Revisar los logs del período entre el restart de Daphne y este deploy, buscando respuestas 400 con `"error_type": "RecaptchaFailed"` en los 4 endpoints — para dimensionar si hubo usuarios reales afectados.
2. Verificar en el panel de Google Cloud (Fraud Defense → Keys) que después de este deploy empiezan a llegar assessments reales de los 4 flujos.

## Cómo se verificó

Sin acceso a un entorno con Postgres/Redis disponible para correr el flujo end-to-end real, se verificó:

1. `py_compile` sobre `appConfig.py` y `wind/views.py` — limpio.
2. `pyflakes` sobre los mismos archivos — sin advertencias nuevas (una preexistente en `wind/views.py:189`, no relacionada).
3. `python3 manage.py check` — "System check identified no issues".
4. Los 4 templates se cargaron y renderizaron con Django (`get_template().render()`), con dos variantes de contexto (`recaptcha_site_key` presente y vacía) — confirmando que las etiquetas `{% if %}`/`{% endif %}` balancean y que con la key vacía el `<script src="...recaptcha/api.js...">` no se emite (`grep` confirmó 0 ocurrencias en ese caso) y `RECAPTCHA_SITE_KEY` en JS queda como cadena vacía.
5. Los bloques `<script>` inline extraídos de los 8 renders (4 templates × 2 contextos) se parsearon con `@babel/parser` — los 8 sin errores de sintaxis.

Pendiente (bloqueado por falta de entorno completo): prueba real en navegador confirmando que la llamada de red efectivamente incluye `recaptcha_token`, y una prueba end-to-end con `RECAPTCHA_SECRET_KEY` real contra el panel de Google, como sugiere la sección "Verificación sugerida" del plan.

## Nota sobre alcance

Esto cubre las 4 páginas servidas por el propio backend Wind (`register.html`, `forgot-password.html`, `reset-password.html`, `dashboard.html`). El "olvidé contraseña" y "eliminar cuenta" nativos de appVideo (`src/services/accountSecurityService.js` + `src/services/recaptchaService.js`, implementados antes en esta misma sesión) llaman a los mismos endpoints (`/api/auth/password/forgot/` y `/api/v1/profile/account/close/`) y ya mandan `recaptcha_token` de forma independiente — ambas superficies (dashboard web y appVideo) protegen el mismo backend. El registro y el restablecimiento de contraseña no tienen equivalente nativo en appVideo (siguen siendo externos a esa app), así que este deploy es lo único que los cubre.

Sigue fuera de alcance (ver sección 5 del plan): iOS/Android nativos.
