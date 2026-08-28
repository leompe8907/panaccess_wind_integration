# Plan de activación de reCAPTCHA (Alto #7)

Fecha: 2026-08-26
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (Alto #7), `docs/RECAPTCHA_ESTADO_Y_PENDIENTES.md` (estado detallado), `docs/GUIA_CREAR_LLAVES_RECAPTCHA_2026-08-26.md` (cómo conseguir las llaves)
Estado: **Plan, sin implementar todavía.**

## Punto de partida

El backend ya está completamente listo en los 4 endpoints -- todos llaman `verify_recaptcha()` y esperan el mismo campo `recaptcha_token` en el body:

| Flujo | Endpoint | Vista |
|---|---|---|
| Registro | `POST /wind/create-subscriber/` | `create_subscriber_view` |
| Olvidé contraseña | `POST /api/auth/password/forgot/` | `password_forgot_view` |
| Restablecer contraseña | `POST /api/auth/password/reset-confirm/` | `password_reset_confirm_view` |
| Eliminar cuenta | `POST /api/v1/profile/account/close/` | `profile_close_account_view` |

Mientras `RECAPTCHA_SECRET_KEY` esté vacía (como está hoy), ninguno de los 4 bloquea nada -- así que activar esto es, del lado backend, **cambiar una sola variable de entorno**. Todo el trabajo real está del lado del frontend (agregar el widget en 4 lugares) y en conseguir las llaves de Google primero.

## Qué falta, en orden

### 1. Conseguir las llaves de Google (bloqueante para todo lo demás)

Ver `docs/GUIA_CREAR_LLAVES_RECAPTCHA_2026-08-26.md`. Necesita una cuenta de Google (idealmente del cliente/empresa, no personal) -- este es un paso que probablemente el cliente tiene que hacer él mismo o autorizar explícitamente, similar al criterio ya aplicado con la config de nginx (no asumir acceso a cuentas del cliente sin que lo pida).

Resultado esperado: una **site key** (pública) y una **secret key** (privada, la "legacy key").

### 2. Agregar el widget en los 4 formularios (frontend, código de este repo)

Para cada uno: cargar el script de Google con la site key, ejecutar `grecaptcha.execute()` justo antes de enviar el formulario (no en `onload` -- los tokens expiran a los 2 minutos), y agregar `recaptcha_token: token` al body JSON que ya arma cada formulario.

- **`wind/templates/wind/register.html`** -- el `<form id="registerForm">` ya tiene un handler de `submit` (línea ~728) que arma el body y llama `fetch(apiPath, ...)` (línea ~747). Agregar la llamada a `grecaptcha.execute()` justo antes de ese `fetch`, y sumar `recaptcha_token` al payload.
- **`wind/templates/wind/forgot-password.html`** -- mismo patrón: agregar antes del `fetch` a `/api/auth/password/forgot/`.
- **`wind/templates/wind/reset-password.html`** -- mismo patrón: agregar antes del `fetch` a `/api/auth/password/reset-confirm/` (el que ya se tocó en el cambio del toggle mostrar/ocultar contraseña).
- **`wind/templates/wind/dashboard.html`** -- el modal de "Eliminar cuenta" (`close-account`) ya arma un body con `code`/`confirm`/`reason`/`dry_run` hacia `/api/v1/profile/account/close/` -- agregar `recaptcha_token` ahí también.

Patrón sugerido para cada template (usando `data-action` distinto por formulario, siguiendo la recomendación de Google de nombrar acciones):

```html
<script src="https://www.google.com/recaptcha/api.js?render=<SITE_KEY>"></script>
<script>
  function getRecaptchaToken(action) {
    return new Promise((resolve, reject) => {
      grecaptcha.ready(() => {
        grecaptcha.execute("<SITE_KEY>", { action }).then(resolve).catch(reject);
      });
    });
  }
</script>
```

Y en cada handler de `submit`, antes del `fetch`:

```js
const recaptcha_token = await getRecaptchaToken("register"); // "forgot_password" / "reset_password" / "close_account" según el formulario
```

La `<SITE_KEY>` puede quedar hardcodeada en cada template (es pública, no hay problema), o mejor, pasada desde el backend como contexto de la vista (mismo patrón que `google_client_id`/`facebook_app_id` en `login.html`) -- así el día que se rote la llave no hace falta tocar 4 archivos HTML, solo la variable de entorno. **Recomendado: la segunda opción**, agregando `RecaptchaConfig.SITE_KEY` (nueva variable, ver siguiente punto) y pasándolo por contexto en las 4 vistas que renderizan estos templates.

### 3. Nueva variable de configuración: la site key

Hoy `RecaptchaConfig` solo tiene `SECRET_KEY` (privada). Falta agregar la pública, para poder inyectarla en los templates sin hardcodearla:

```python
class RecaptchaConfig:
    SECRET_KEY = _strip_env(os.getenv("RECAPTCHA_SECRET_KEY"))
    SITE_KEY = _strip_env(os.getenv("RECAPTCHA_SITE_KEY"))
    MIN_SCORE = float(os.getenv("RECAPTCHA_MIN_SCORE", "0.5"))
```

Y agregar `RECAPTCHA_SITE_KEY=` (vacío hasta tener la llave real) a `.env`, junto a `RECAPTCHA_SECRET_KEY`.

### 4. Orden de activación -- importante para no romper nada

1. Conseguir las llaves (paso 1).
2. Configurar **solo** `RECAPTCHA_SITE_KEY` en `.env` y desplegar el frontend con el widget en los 4 formularios. En este punto, el backend **todavía no exige** el token (`RECAPTCHA_SECRET_KEY` sigue vacía) -- los 4 formularios ya mandan `recaptcha_token`, pero si algo falla o no llega, no se rechaza nada. Sirve como período de prueba real sin riesgo.
3. Verificar en el panel de Google Cloud (Fraud Defense → Keys) que están llegando assessments reales de los 4 flujos, con scores razonables.
4. Recién ahí, configurar `RECAPTCHA_SECRET_KEY` y reiniciar Daphne -- a partir de este punto los 4 flujos exigen el token de verdad.

### 5. Mobile (iOS/Android) -- fuera del alcance de este plan

Ya señalado en el hallazgo original y en "Posibles mejoras #31": las apps también necesitan su propia integración (site key de tipo mobile + SDK nativo) para "olvidé contraseña" y "eliminar cuenta" antes de considerar esto cerrado del lado mobile. El backend ya soporta esto sin cambios adicionales (mismo endpoint, mismo campo `recaptcha_token`) -- lo que falta es trabajo en los repos de las apps, fuera de este proyecto.

## Riesgos / cosas a tener en cuenta

- **No activar `RECAPTCHA_SECRET_KEY` antes de que el frontend mande el token** -- si se hace en el orden equivocado, los 4 flujos (registro, olvidé contraseña, restablecer contraseña, eliminar cuenta) empiezan a rechazar el 100% de los intentos, incluida gente real.
- **Cuota gratuita:** 10,000 verificaciones/mes. Si Wind tiene mucho tráfico de registro, vale la pena revisar el panel de Google después de la primera semana activa para confirmar que no se acerca al límite (recordar: pasarse del límite no bloquea nada -- "fail open" -- pero sí significa que esos meses quedan sin protección real de bots).
- **`RECAPTCHA_MIN_SCORE=0.5`** es el default recomendado por Google para arrancar -- ajustar según los scores reales que se vean en el panel, no a ciegas.

## Verificación sugerida (cuando se implemente)

1. `py_compile` + `manage.py check` sobre los archivos tocados.
2. Con `RECAPTCHA_SECRET_KEY` vacía todavía: confirmar que los 4 formularios mandan `recaptcha_token` en la request (inspeccionando la llamada de red desde el navegador) y que el flujo sigue funcionando igual que hoy.
3. En un entorno de prueba, configurar una `RECAPTCHA_SECRET_KEY` real y probar: un envío legítimo (debe pasar), y confirmar en el panel de Google que aparece el assessment con un score.
4. Recién con eso confirmado, replicar en producción siguiendo el orden del punto 4 de arriba.

## Actualización pendiente en la auditoría

Cuando esto se implemente, actualizar la fila Alto #7 de `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` y la "Posibles mejoras #31" si se coordina también el lado mobile.
