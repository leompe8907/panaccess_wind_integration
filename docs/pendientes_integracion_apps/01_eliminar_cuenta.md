# Eliminar cuenta (cierre inmediato + eliminación con demora)

Estado: backend listo para ambos flujos. Cierre inmediato ya integrado en appVideo. Eliminación con demora (la nueva, pedida por el cliente) **ya está implementada en appVideo (2026-09-17)**; iOS y Android todavía no la tienen.
Referencia completa: `docs/GUIA_INTEGRACION_UNIFICADA.md` secciones 5.3 y 5.4, `docs/NUEVO_FLUJO_ELIMINACION_CUENTA_2026-09-08.md` (diseño, corrección 2026-09-09, y addendum 2026-09-17 con el paso de confirmación de correo).

## Son dos flujos distintos, no uno solo

| | Cierre inmediato (5.3) | Eliminación con demora (5.4, nuevo) |
|---|---|---|
| Endpoint | `POST /api/v1/profile/account/close/` | `POST /api/v1/profile/account/close/request/` |
| Cuándo corta el acceso | Al toque, en la misma llamada | Recién en la fecha de corte de la suscripción actual -- el usuario sigue usando la cuenta con normalidad hasta entonces |
| Confirmación | Ninguna, es directo | Requiere que el usuario confirme por un link que llega por correo (24h de vigencia) |
| Uso típico | "Cerrar cuenta ya" | Mockup del cliente: "Mi cuenta > Eliminar cuenta" |

Ambos requieren JWT + reCAPTCHA (ver `08_recaptcha.md` para la limitación de reCAPTCHA en mobile nativo). Ninguno reemplaza al otro -- el endpoint viejo (5.3) sigue funcionando igual que siempre.

## Contrato: cierre inmediato (5.3)

`POST /api/v1/profile/account/close/`, body `{"code","confirm" (=code),"reason","dry_run"}`. `dry_run:true` no borra nada, solo devuelve un plan. Éxito: `{"success":true,"subscriber_code","panaccess":{...},"local":{...,"device_sessions_revoked":N,"udid_revoked":N},"closure_log_id","re_registration":"allowed_without_trial",...}`. Cierre parcial (PanAccess falló pero el acceso local ya se cortó): `{"success": false, ...}` -- el corte de acceso local (JWT, dispositivos, pareos) ocurre siempre, incluso si PanAccess no terminó.

## Contrato: eliminación con demora (5.4)

**Paso 1 -- pedir la eliminación (dentro de la app, con JWT):** `POST /api/v1/profile/account/close/request/` (JWT + reCAPTCHA), body `{"code": "<subscriber_code>", "email": "<correo de la cuenta>", "reason": "<opcional>"}`. **`email` es obligatorio desde 2026-09-17** (a pedido del cliente -- casos de usuarios "curiosos" que eliminaban su cuenta sin querer, mismo patrón de confirmación que cambio de contraseña, ver `02_cambiar_contrasena.md`): el usuario re-escribe el correo de su cuenta como paso extra de fricción antes de que salga el correo con el link. El backend valida que coincida (case-insensitive) con el correo real de la cuenta -- si no coincide, no manda nada.

No corta nada ni toca PanAccess -- solo manda un correo con un link de confirmación. Éxito: `{"success": true, "message": "...", "masked_email": "u**r@example.com", "scheduled_for": "<ISO 8601 o null>"}` -- `scheduled_for` (agregado 2026-09-17) es la fecha de corte actual de la suscripción (`lastExpiryTime`), ya disponible en este paso, antes de que el usuario confirme nada; puede venir `null` si el suscriptor nunca la sincronizó desde PanAccess. Si no se puede procesar: `{"success": false, "code": "already_closed"|"closure_already_scheduled"|"no_email_on_file"|"email_mismatch", "message": "...", "scheduled_for": "<ISO 8601, solo con closure_already_scheduled>"}`. Llamarlo de nuevo antes de confirmar simplemente reenvía el mismo correo (no crea una segunda solicitud).

**Paso 2 -- confirmar:** pasa 100% en una página web del backend (`/wind/eliminar-cuenta/confirmar/?t=<token>`) a la que el usuario llega desde el link del correo -- **ningún cliente (ni appVideo ni mobile) llama a ningún endpoint para este paso.** Al confirmar, el backend programa la fecha de corte pero no cambia nada del acceso todavía; la cuenta sigue `ACTIVE` hasta esa fecha.

**Mientras tanto:** `GET /api/v1/profile/me/` sigue devolviendo la cuenta con normalidad, sin ningún campo que indique "eliminación programada". Si el producto quiere mostrar "tu cuenta se elimina el `<fecha>`" en algún momento posterior a haberla pedido, hoy esa fecha solo se conoce por el `scheduled_for` que devuelve el paso 1 en el momento de pedirla (o por el correo) -- no hay todavía un endpoint para consultarla después.

**Cancelar una eliminación ya confirmada:** no implementado (el cliente de negocio no confirmó si hace falta).

## Estado en appVideo (web)

Cierre inmediato: implementado (`closeAccount()` en `accountSecurityService.js`, UI en `CloseAccountPanel.jsx`, sin usar desde 2026-09-17). Eliminación con demora: **implementada (2026-09-17)** -- `requestAccountDeletion()` en `accountSecurityService.js`, UI en `CloseAccountPanel.jsx` reescrito en 4 pasos (checkbox de advertencia -> popup "¿estás seguro?" -> re-escribir correo -> pantalla "revisa tu correo" con `masked_email`/fecha de corte y botón de reenviar). Ver addendum 2026-09-17 en `docs/NUEVO_FLUJO_ELIMINACION_CUENTA_2026-09-08.md` para el detalle archivo por archivo.

## Qué debe implementar iOS/Android nativo

Exactamente el mismo contrato REST que appVideo -- no hay diferencia de plataforma en estos endpoints, ambos son JSON sobre HTTPS con el mismo JWT de sesión:

- Si la app ya tiene una pantalla de "cerrar cuenta" apuntando al endpoint inmediato (5.3), decidir junto con producto si esa pantalla pasa a usar el flujo con demora (5.4) o si conviven ambas opciones.
- Implementar la llamada a `POST /api/v1/profile/account/close/request/` con JWT + reCAPTCHA + **`email`** (obligatorio desde 2026-09-17, ver contrato arriba) -- **la limitación de reCAPTCHA en apps nativas (sin WebView) aplica acá igual que en "olvidé contraseña"**, ver `08_recaptcha.md` antes de asumir que el token siempre se puede generar.
- Si el producto quiere la misma fricción en capas que se armó para appVideo (checkbox + popup + re-escribir correo antes de pedir el enlace), replicarla es puramente de UI nativa -- el backend ya exige el `email` sin importar qué pantallas lo rodeen.
- La pantalla de éxito debe dejar claro que la cuenta sigue activa hasta la fecha de corte (ahora disponible directo en la respuesta del paso 1, campo `scheduled_for`, sin necesidad de esperar al correo) -- no mostrar "cuenta eliminada" ni cerrar la sesión localmente, porque el backend no lo hizo.
- Si el producto quiere mostrar la fecha de corte más adelante (no solo en el momento de pedirla), avisar a backend -- hoy no hay endpoint para consultarla después.
- El paso de confirmación (click en el correo) no requiere ningún trabajo nativo: pasa en el navegador del sistema o en el cliente de correo, nunca dentro de la app.

## Pendientes / decisiones abiertas

- Cancelar una eliminación ya confirmada -- sin definir si hace falta.
- Endpoint para consultar la fecha programada después del momento de pedirla -- no existe, avisar si se necesita.
