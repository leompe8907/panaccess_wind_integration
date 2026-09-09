# Nuevo flujo de eliminación de cuenta (confirmación por correo + ejecución diferida)

Fecha: 2026-09-08 (corregido 2026-09-09, ver abajo)
Contexto: mockups del cliente para "Mi cuenta > Eliminar cuenta". Respuesta del cliente sobre el timing: *"Si el decide eliminar hoy, pero le quedan 5 días de uso del servicio, debería de esperar esos 5 días para que se ejecute"*.

## CORRECCIÓN 2026-09-09: el acceso ya NO se corta al confirmar por correo

La primera versión de este flujo (2026-09-08, descripta más abajo tal como quedó documentada en su momento) cortaba el acceso -- `status=PENDING_CLOSURE`, `User.is_active=False`, JWT invalidado, UDID/dispositivos revocados -- en el mismo momento en que el usuario confirmaba por correo (paso 2 de abajo). Eso contradice justo lo que pidió el cliente: el abonado debe poder seguir usando el servicio con normalidad durante los días que le queden hasta la fecha de corte, no perder el acceso apenas confirma.

Se corrigió: `confirm_account_deletion()` ya **no** llama a `cut_subscriber_access_and_tombstone()`. Ahora, al confirmar, solo se guarda `ListOfSubscriber.scheduled_closure_at = lastExpiryTime` -- el `status` se queda en `ACTIVE` y el usuario sigue con acceso normal (login, JWT, dispositivos vinculados, todo intacto). El corte de acceso real (la mitad que antes corría en el momento de confirmar) pasa a ejecutarse recién cuando `retry_partial_closures_task` dispara el cierre de verdad en la fecha de corte -- reusando `close_subscriber_account()` tal cual, que internamente sigue llamando a `cut_subscriber_access_and_tombstone()` en ese momento (esa función no cambió; lo que cambió es *cuándo* se invoca).

Como consecuencia, el resto de este documento (pasos 2-3, la sección "Qué NO se implementó", "Cómo se verificó") describe el diseño **original, ya superado** en el punto del corte de acceso -- se deja tal cual por valor histórico, pero donde diga "se corta el acceso" al confirmar, o "queda en PENDING_CLOSURE" al confirmar, léase: el abonado se queda en `ACTIVE`, con acceso normal, hasta la fecha de corte.

## Qué cambia respecto al flujo actual

Hoy (`profile_close_account_view`, `POST /api/v1/profile/account/close/`) el usuario escribe su código para confirmar y la cuenta se cierra **al toque**: PanAccess se desaprovisiona en el mismo request. **Ese endpoint sigue existiendo tal cual, sin tocar nada** -- queda disponible para cierre inmediato/uso interno.

El nuevo flujo es aditivo, en endpoints separados:

1. El usuario pide eliminar desde la app (checkbox + modal "¿estás seguro?", ya sin escribir el código) -> `POST /api/v1/profile/account/close/request/`. Esto **no toca nada todavía**: solo manda un correo con un enlace de confirmación (24h de vigencia).
2. El usuario confirma haciendo click en el correo -> `GET`/`POST /wind/eliminar-cuenta/confirmar/?t=<token>` (página web, no es parte de la app). Ahí recién se corta el acceso: se desactiva el `User` del portal, se invalidan JWT, se revocan UDID y dispositivos vinculados -- exactamente el mismo mecanismo que ya usaba el cierre inmediato (`cut_subscriber_access_and_tombstone`, extraído de `close_subscriber_account` para reusarlo acá sin duplicar lógica).
3. La desaprovisión real en PanAccess **no ocurre en este momento**. Se guarda `ListOfSubscriber.scheduled_closure_at = lastExpiryTime` (la fecha de corte real de la suscripción, ya sincronizada desde PanAccess) y el abonado queda en `PENDING_CLOSURE`.
4. `wind.tasks.retry_partial_closures_task` -- la tarea de Celery Beat que **ya existía** para reintentar cierres que quedaron a medias (corre cada `CLOSURE_RETRY_MINUTES`, 30 min por defecto) -- ahora también respeta `scheduled_closure_at`: si la fecha todavía no llegó, salta esa fila; apenas llega, la ejecuta como cualquier otro reintento. **No hizo falta crear una tarea nueva ni un Beat schedule nuevo**, solo un filtro extra en la consulta que ya existía.

Esto significa que la ejecución real puede tardar hasta `CLOSURE_RETRY_MINUTES` después de la fecha de corte exacta (no al segundo) -- si el cliente necesita precisión al minuto, hay que bajar ese intervalo (`.env`, `CELERY_CLOSURE_RETRY_MINUTES`) o pedir una tarea dedicada; no se hizo porque no se pidió esa precisión.

## Reenviar correo

El mismo endpoint (`POST .../account/close/request/`) reenvía si se llama de nuevo con el mismo `code` mientras no se haya confirmado: reusa la fila (`AccountDeletionRequest`), no crea una solicitud duplicada, solo re-firma un token nuevo (otras 24h).

## Qué NO se implementó todavía (pendiente)

- **Frontend de appVideo**: `accountSecurityService.js::closeAccount()` sigue apuntando al endpoint viejo de cierre inmediato. Falta: los 3 paneles del mockup (advertencia + checkbox, modal de confirmación, pantalla "revisa tu correo" con reenviar), y apuntar `closeAccount()` al nuevo endpoint `/account/close/request/`.
- **Cancelar un cierre programado**: el cliente no confirmó si el usuario puede arrepentirse después de confirmar por correo. Hoy, una vez confirmado, no hay manera de revertirlo desde la app ni la API -- solo a mano (borrar `scheduled_closure_at` y devolver `status` a `active`). Si el cliente lo pide más adelante, es un endpoint chico de agregar (mismo patrón: validar dueño, limpiar `scheduled_closure_at`, volver a `STATUS_ACTIVE`).
- **Diseño real del correo y de la página de confirmación web**: se armaron con el mismo estilo visual que ya usa el correo/página de reset de contraseña (banner, botón, aviso de expiración), con la copia del mockup. Los badges de Google Play/App Store del mockup son por ahora links de texto (`EmailConfig.GOOGLE_PLAY_URL`/`APP_STORE_URL`) -- no hay infraestructura de imágenes de badges en los correos todavía, se puede agregar cuando haya arte aprobado.
- **reCAPTCHA en la página de confirmación web** (`/wind/eliminar-cuenta/confirmar/`): no se agregó -- la posesión del token ya autoriza la acción (un solo click, sin más datos que recolectar), mismo criterio que otros links de un solo uso. Se puede sumar si se considera necesario.
- **Subscriptores sin `lastExpiryTime` sincronizado** (actualizado 2026-09-09): si PanAccess nunca informó una fecha de expiración para ese abonado (dato no disponible), la confirmación deja `scheduled_closure_at = now()` (en vez de `None`) para que el próximo ciclo de `retry_partial_closures_task` lo ejecute -- el filtro nuevo de esa tarea exige `scheduled_closure_at` no nulo para las filas `ACTIVE`, así que un `None` ahí se quedaría confirmado para siempre sin disparar nunca el cierre. Se loguea un `WARNING` para poder detectarlo. No debería pasar en la práctica (todo abonado con producto activo tiene esa fecha), pero queda cubierto para no dejar una cuenta colgada para siempre.

## Archivos nuevos

- `wind/models.py`: campo `ListOfSubscriber.scheduled_closure_at`; modelo `AccountDeletionRequest`.
- `wind/migrations/0012_scheduled_account_deletion.py`.
- `wind/services/account_deletion.py`: tokens firmados (`TimestampSigner`, salt propio, 24h), `request_account_deletion()`, `confirm_account_deletion()`.
- `wind/services/account_deletion_email.py` + `wind/tasks.py::send_account_deletion_confirmation_email_task` + plantillas `wind/templates/wind/emails/account_deletion_confirmation.{html,txt}`.
- `wind/api/profile/views.py::profile_request_account_deletion_view` (+ serializer `ProfileRequestAccountDeletionSerializer`, + URL `POST /api/v1/profile/account/close/request/`).
- `wind/views.py::delete_account_confirm_view` + `wind/templates/wind/delete-account-confirm.html` + URL `/wind/eliminar-cuenta/confirmar/`.
- `wind/tests/test_account_deletion_scheduled.py` (15 tests nuevos).

## Archivos modificados

- `wind/services/subscriber_closure.py`: se extrajo `cut_subscriber_access_and_tombstone()` de `close_subscriber_account()` (mismo comportamiento de siempre, ahora reusable); al cerrar de verdad, limpia `scheduled_closure_at` si venía de una eliminación programada.
- `wind/tasks.py::retry_partial_closures_task`: filtro nuevo por `scheduled_closure_at` (ver arriba).
- `appConfig.py`: `EmailConfig.ACCOUNT_DELETION_CONFIRM_SUBJECT`, `ACCOUNT_DELETION_BANNER_IMAGE_URL`, `ACCOUNT_DELETION_CONFIRM_LINK_BASE_URL`.

## Cómo se verificó

- `manage.py check` OK.
- Versión original (2026-09-08): 15 tests nuevos + los 3 existentes de `test_subscriber_closure.py` (para confirmar que el refactor de `close_subscriber_account` no cambió su comportamiento) + los 2 de `test_register_view_feature_flag.py`: 21/21 OK contra Postgres real (`pgserver`). El usuario confirmó 150/150 en su máquina tras este punto.
- Corrección 2026-09-09: se reescribieron los tests afectados de `test_account_deletion_scheduled.py` (ahora 19, antes 15 -- se sumaron los casos `ACTIVE`+vencido y `ACTIVE`+futuro para `retry_partial_closures_task`) para reflejar que confirmar ya NO corta el acceso. Se corrió `wind.tests.test_account_deletion_scheduled` + `test_subscriber_closure` + `test_go_windtv_view` + `test_subscriber_sync_closure` + `test_auth` (35 tests): **32/32 OK**, los otros 3 son el error de conexión a Redis ya conocido del sandbox (sin Redis local ahí, no relacionado a este cambio) -- pendiente que el usuario corra `deploy\run_tests_local.bat` para la confirmación final de la suite completa con Redis real.
