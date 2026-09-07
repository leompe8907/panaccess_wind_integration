# Desactivación temporal de reCAPTCHA (decisión de negocio, 2026-09-03)

Fecha: 2026-09-03
Referencia: `docs/RECAPTCHA_WIDGET_4_FORMULARIOS_2026-09-01.md` y `docs/RECAPTCHA_LOGIN_Y_CAMBIO_PASSWORD_2026-09-01.md` (activación original), `docs/GUIA_RECAPTCHA_MOBILE_IOS_ANDROID_2026-09-03.md` (por qué Wind iOS no puede mandar el token todavía), `docs/RECAPTCHA_ESTADO_Y_PENDIENTES.md` (doc de estado, actualizado con esta desactivación).

## Qué

El equipo de Wind iOS reportó que `RECAPTCHA_SECRET_KEY` está activa en producción desde el 2026-09-01, y que la app **nunca implementó** el envío de `recaptcha_token` en ninguno de los 3 endpoints que la app llama y que hoy lo exigen: login (`POST /api/auth/login/`), cambiar contraseña (`POST /api/v1/profile/password/`) y cerrar cuenta (`POST /api/v1/profile/account/close/`). Confirmado contra el código real (`recaptcha_required()` devuelve `True` con la secret key configurada) y contra el `.env` real de este repo.

El equipo iOS no va a implementar el SDK nativo de reCAPTCHA (Google Cloud Fraud Defense) hasta que el backend soporte la Assessment API para verificar esos tokens (ver `docs/GUIA_RECAPTCHA_MOBILE_IOS_ANDROID_2026-09-03.md`, Paso 4) -- trabajo todavía sin dimensionar ni agendar. Mientras se resuelve eso, decisión del cliente: **desactivar reCAPTCHA por completo** en vez de aplicar un fix acotado (se había propuesto dejarlo obligatorio solo en registro/olvidé-contraseña y opcional en los 3 que usa iOS -- se descartó esa opción a favor de la desactivación total, ya que el proyecto sigue en pruebas internas y el riesgo de bots no es una preocupación en esta etapa).

## Cómo

Un solo cambio: `.env`, `RECAPTCHA_SECRET_KEY` se vació (antes tenía un valor real). `recaptcha_required()` (`wind/utils/recaptcha.py`) vuelve a su comportamiento opt-in por defecto -- con la secret key vacía, `verify_recaptcha()` devuelve `(True, None)` sin evaluar nada, para los 6 endpoints, para cualquier cliente (login, cambio de contraseña, cerrar cuenta, registro, olvidé contraseña, restablecer contraseña). No se tocó ningún archivo de código -- el mecanismo de "apagado total" ya existía desde el diseño original (`wind/utils/recaptcha.py`: *"Opt-in: si RECAPTCHA_SECRET_KEY no está configurado, verify_recaptcha no bloquea nada"*).

El valor real de la secret key se guardó comentado en el propio `.env` (línea justo arriba de `RECAPTCHA_SECRET_KEY=`) para no perderlo cuando se decida reactivar.

## Por qué

- **Urgente:** con la secret key activa, cualquier login/cambio de contraseña/cierre de cuenta real desde Wind iOS estaba (o va a estar, apenas se arregle el certificado TLS roto de `backend.wind.do`) fallando con 400 `RecaptchaFailed` -- afecta las 3 acciones más básicas de la app.
- **Por qué desactivar todo en vez del fix acotado (registro/olvidé-contraseña obligatorio, resto opcional):** decisión explícita del cliente -- el proyecto sigue en desarrollo con solo pruebas internas, así que el riesgo de bots creando cuentas masivamente en `/wind/create-subscriber/` (la motivación original de todo esto) no es una amenaza real todavía. Se prioriza no romper nada por sobre mantener una protección que hoy no tiene a quién detener.
- **No es la solución de fondo:** el problema real (backend solo soporta verificación clásica `siteverify`, incompatible con los tokens que produciría el SDK nativo de iOS/Android) sigue sin resolverse -- ver `docs/GUIA_RECAPTCHA_MOBILE_IOS_ANDROID_2026-09-03.md`. Esto solo destraba el desarrollo mientras se decide cuándo encarar ese trabajo.

## Verificado

```
RecaptchaConfig.SECRET_KEY == ''
recaptcha_required() == False
```

Confirmado ejecutando el código real con el `.env` actual (sin overrides), `django.setup()` completo. No se corrieron los tests completos del proyecto para esto porque no es un cambio de código -- es una variable de configuración, y el comportamiento con secret key vacía es exactamente el "modo apagado" original que ya estaba cubierto por los tests existentes de cada endpoint (todos escritos para correr con reCAPTCHA deshabilitado por default).

## Antes de reactivar (checklist para la próxima vez)

1. Confirmar que Wind iOS (y Android si aplica) ya manda `recaptcha_token` en los 3 endpoints, o decidir explícitamente activar el fix acotado (obligatorio solo en registro/olvidé-contraseña) en vez de la reactivación total.
2. Si se reactiva total: restaurar `RECAPTCHA_SECRET_KEY` al valor guardado comentado en `.env` (`6Lc9w6MtAAAAAIphEtpDtdJXnbrxVin1gDW_y2rz`).
3. Confirmar que appVideo/web siguen mandando el token correctamente (no debería haber cambiado nada del lado de esos clientes en este período).
4. Actualizar `docs/RECAPTCHA_ESTADO_Y_PENDIENTES.md` y el hallazgo correspondiente si quedó alguno abierto en `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`.

## Archivos tocados

- `.env` (`RECAPTCHA_SECRET_KEY` vaciada, valor real preservado en comentario)
- `docs/RECAPTCHA_ESTADO_Y_PENDIENTES.md` (nota de actualización)
