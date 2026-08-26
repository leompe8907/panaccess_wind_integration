# Integración "Eliminar cuenta" para apps (iOS / Android)

Fecha: 2026-08-25
Para: equipos de apps iOS / Android
Backend: `POST /api/v1/profile/account/close/`

## Resumen

Ya existe en el backend un endpoint que elimina la cuenta del suscriptor autenticado de punta a punta: da de baja en PanAccess, cierra la sesión localmente, revoca todos los dispositivos vinculados, y deja el registro marcado como cerrado. No hace falta ningún cambio en el backend para que las apps lo integren -- este documento describe el contrato tal cual está implementado hoy.

## Endpoint

```
POST /api/v1/profile/account/close/
```

**Auth:** JWT, header `Authorization: Bearer <access_token>` (el mismo access token de sesión que ya usan para el resto del perfil).

**Throttle:** 120 requests/minuto por usuario (`ProfileThrottle`) -- de sobra para este flujo, no debería afectar el uso normal.

### Body (JSON)

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `code` | string | Sí | Código de suscriptor PanAccess del usuario logueado. Es el mismo valor que ya reciben en `GET /api/v1/profile/me/` como `subscriber_code`. |
| `confirm` | string | Sí | Debe ser **idéntico** a `code`. Es una confirmación explícita del lado del cliente -- pensado para forzar a la app a mostrar una pantalla de "escribe/confirma tu código" o al menos a repetir el valor deliberadamente, no un doble-tap accidental. |
| `reason` | string | No | Texto libre, máx. 500 caracteres. Si se envía vacío o se omite, el backend guarda `"user_dashboard_close"`. Útil si la app quiere ofrecer un selector de motivo ("ya no la uso", "cambio de proveedor", etc.) antes de confirmar. |
| `dry_run` | boolean | No | Default `false`. Si se manda `true`, el backend simula todo el proceso (incluida la consulta a PanAccess) **sin ejecutar ningún cambio real** -- ni en PanAccess, ni local, ni cierre de sesión. Pensado para un botón de "verificar antes de confirmar" si la app quiere mostrarle al usuario qué va a pasar antes del punto de no retorno. |
| `recaptcha_token` | string | Condicional | Solo se valida si el backend tiene reCAPTCHA habilitado (hoy está desactivado en producción -- si se activa más adelante, este campo pasa a ser obligatorio y sin él el request falla). Recomendado enviarlo siempre que la app ya tenga el SDK de reCAPTCHA integrado, para no tener que hacer un release nuevo el día que se active. |

Nota importante: **`code` también viaja implícito en el permiso**. El backend valida que el `code` enviado corresponda al usuario autenticado por el JWT (`IsOwnerSubscriber`) -- si no coincide, o si falta, el request se rechaza con `403` antes de llegar a cualquier lógica de negocio. No es posible eliminar la cuenta de otro suscriptor aunque se tenga su código.

### Ejemplo de request

```http
POST /api/v1/profile/account/close/
Authorization: Bearer eyJ0eXAiOiJKV1Qi...
Content-Type: application/json

{
  "code": "SUB123456",
  "confirm": "SUB123456",
  "reason": "ya no uso el servicio"
}
```

## Qué hace el backend al recibirlo (para entender el impacto en la app)

1. Marca la cuenta como "en cierre" de inmediato (tombstone), antes de tocar nada más.
2. Desactiva el usuario local e **invalida el JWT de la sesión actual y de cualquier otra sesión activa** -- cualquier otro dispositivo logueado con la misma cuenta queda deslogueado.
3. Revoca todos los pareos de TV/dispositivos vinculados (`DeviceSession`, `UDIDAuthRequest`).
4. Llama a PanAccess en cadena: consulta órdenes → quita bloqueo de licencia → deshabilita orden → limpia smartcards → quita smartcard → elimina el suscriptor.
5. Borra los datos operacionales locales del suscriptor (productos, smartcards en caché, etc.), pero **conserva** el registro de email/documento marcado como "cuenta cerrada" -- esto es a propósito: permite que la misma persona se registre de nuevo más adelante, aunque sin derecho a un nuevo período de prueba.

**Es inmediato, no hay período de gracia ni "cancelar en los próximos X días".** Una vez que el backend responde éxito, el acceso ya está cortado.

## Respuestas

| Status | Cuerpo (resumen) | Qué debería hacer la app |
|---|---|---|
| `200` éxito | `{"success": true, "subscriber_code", "panaccess", "local", "closure_log_id", "re_registration": "allowed_without_trial", "message"}` | Cerrar sesión localmente (borrar tokens guardados), mostrar confirmación, llevar al usuario a la pantalla de login/landing. |
| `200` ya estaba cerrada | `{"success": true, "already_closed": true, "subscriber_code", "message"}` | Tratar igual que éxito -- cerrar sesión localmente. Puede pasar si el usuario reintenta o si ya la cerró desde otro dispositivo. |
| `200` dry run | `{"success": true, "dry_run": true, "subscriber_code", "panaccess", "local_plan"}` | Solo si la app mandó `dry_run: true`. No cerrar sesión -- nada se ejecutó de verdad. Usar para mostrar una pantalla de "esto es lo que va a pasar" antes de la confirmación real. |
| `400` | `{"success": false, "errors": {...}}` o `{"success": false, "error_type": "RecaptchaFailed", "message": "..."}` | Error de validación (falta `code`/`confirm`, no coinciden, o falla reCAPTCHA). Mostrar el mensaje y permitir reintentar. |
| `403` | `{"detail": "No puede operar sobre otro suscriptor."}` (o feature apagado: `{"success": false, "message": "El cierre de cuenta desde el dashboard está deshabilitado."}`) | No debería pasar en uso normal salvo que el `code` local esté desincronizado -- si pasa, refrescar `GET /api/v1/profile/me/` para obtener el `code` correcto antes de reintentar. |
| `502` | `{"success": false, "subscriber_code", "panaccess", "closure_log_id", "message": "Cierre parcial en PanAccess; reintente o revise logs."}` | El cierre quedó a medias del lado de PanAccess (poco común, pero posible por caída puntual del servicio). **El acceso local ya se cortó igual** (la sesión ya fue invalidada), así que conviene cerrar sesión localmente de todos modos y sugerir reintentar más tarde o contactar soporte -- no queda "como si nada hubiera pasado". |
| `500` | `{"success": false, "message": "Ocurrió un error inesperado al eliminar la cuenta. Intenta de nuevo."}` | Error inesperado no relacionado con PanAccess. Permitir reintentar; si persiste, contactar soporte. |

## Recomendación de flujo en la app

1. Pantalla de confirmación clara (esto es irreversible en la práctica: no hay forma de "recuperar" la cuenta desde la app, solo re-registrarse desde cero sin trial).
2. Opcional: llamar primero con `dry_run: true` para validar que todo está en orden antes de mostrar el botón final de confirmar.
3. Llamar sin `dry_run` (o `dry_run: false`) para ejecutar el cierre real.
4. Ante cualquier respuesta `200` (éxito, ya cerrada, o incluso `502` parcial): borrar el JWT guardado localmente y llevar al usuario fuera del área autenticada. El backend ya invalidó la sesión del lado servidor en los primeros tres casos reales de éxito/parcial -- seguir usando el token viejo va a fallar con 401 de todos modos.

## Requisito de Google Play / App Store: enlace público de eliminación de cuenta

Además del endpoint dentro de la app, Google Play (Data Safety) y Apple exigen una **URL pública, accesible sin instalar la app**, donde el usuario pueda solicitar/iniciar la eliminación de su cuenta. Esa página ya existe:

```
https://backend.wind.do/eliminar-cuenta/
```

(vista `delete_account_info_view`, HTML público, sin autenticación). Si algún equipo necesita reportar esta URL en el listing de la store, es esa.

## Notas

- No existe hoy un endpoint separado de "desactivar temporalmente" -- solo cierre definitivo (con posibilidad de re-registro sin trial después). Si el producto quiere ofrecer una pausa reversible, es una funcionalidad nueva, no algo que ya exista para reutilizar.
- El campo `reason` no se muestra a los usuarios en ningún lado; solo queda en el log interno (`SubscriberClosureLog`) para analítica interna.
