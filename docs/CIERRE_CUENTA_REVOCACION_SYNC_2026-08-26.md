# Revocación de dispositivos en el borrado automático + aviso por correo (Alto #4)

Fecha: 2026-08-26
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Alto #4
Estado: **Implementado. Pendiente de verificación end-to-end en un entorno con base de datos real** (este entorno de trabajo no tiene acceso a Postgres -- ver "Verificación hecha" más abajo).

## De qué se trata

`delete_subscriber_operational_data` (`wind/functions/getSubscriber.py`) tiene dos callers:

1. **Cierre manual/API** (`close_subscriber_account`, el mismo flujo documentado para iOS/Android en `docs/INTEGRACION_ELIMINAR_CUENTA_APPS.md`) -- ya revocaba `UDIDAuthRequest`/`DeviceSession` antes de borrar. Sin cambios de comportamiento acá salvo el aviso por correo (ver abajo).
2. **Borrado automático por sincronización** (`_delete_local_subscribers_not_in_remote`, se dispara cuando un suscriptor desaparece del catálogo de PanAccess) -- **no** revocaba nada. Un suscriptor que desaparecía así podía dejar sus dispositivos vinculados (TV, smart TV) autenticando indefinidamente, sin que nadie cortara esa sesión.

Además, ninguno de los dos caminos avisaba al usuario por correo de que su cuenta había sido cerrada.

## Qué se implementó

1. **`wind/functions/getSubscriber.py` -- `delete_subscriber_operational_data`**: los 4 `.delete()` (antes sueltos) ahora corren dentro de `transaction.atomic()`. Si algo falla a mitad de camino, no queda un suscriptor a medio borrar -- beneficia a los dos callers de una sola vez.

2. **`wind/functions/getSubscriber.py` -- `_delete_local_subscribers_not_in_remote`**: antes de borrar, para cada suscriptor que va a eliminarse:
   - Se capturan `email`/`firstName`/`lastName` (de `ListOfSubscriber` y `SubscriberEmailRegistry`) -- tienen que leerse ANTES de borrar, porque el borrado real (`preserve_registry=False`) elimina esas mismas filas.
   - Se revocan `UDIDAuthRequest` (`_revoke_udid_requests`) y `DeviceSession` (`revoke_all_device_sessions_for_subscriber`, razón `"subscriber_deleted_sync"`) -- reutilizando exactamente las mismas funciones que ya usa `close_subscriber_account`, importadas de forma local (`import` dentro de la función) para evitar un import circular, ya que `subscriber_closure.py` importa de este mismo módulo a nivel de archivo.
   - Se encola el correo de aviso (`enqueue_account_closed_email`) con los datos ya capturados.
   - Cada paso está en su propio `try/except` -- ninguno puede frenar el pipeline de sincronización completo si falla para un suscriptor puntual.
   - La respuesta de la función ahora incluye `devices_revoked: {"udid": N, "device_sessions": M}` para poder ver en logs cuántos dispositivos se cortaron en cada corrida.

3. **Correo "cuenta cerrada" nuevo** (mismo patrón que el correo de "contraseña actualizada" ya existente):
   - `wind/templates/wind/emails/account_closed.html` / `.txt`
   - `wind/services/account_closed_email.py`: `build_account_closed_email_context`, `render_account_closed_email_bodies`, `enqueue_account_closed_email` -- esta última nunca lanza (no debe poder tumbar un cierre de cuenta real porque el correo falló), y **se niega a mandar** si no hay un email real (rechaza explícitamente el dominio sintético `@subscribers.wind.local` que usa el sistema como relleno interno cuando no hay contacto real).
   - `wind/tasks.py::send_account_closed_email_task` -- tarea de Celery con reintento (igual que `send_password_changed_email_task`).
   - `appConfig.py::EmailConfig.ACCOUNT_CLOSED_SUBJECT` + `.env::EMAIL_ACCOUNT_CLOSED_SUBJECT`.
   - Conectado en **ambos** caminos: `close_subscriber_account` (al final, solo en el cierre real y completo -- no en `already_closed`, `dry_run`, ni en el caso parcial de PanAccess) y `_delete_local_subscribers_not_in_remote`.

## Decisión sobre el correo en el borrado automático

Se decidió mandar el mismo aviso en los dos caminos (manual y automático), sin distinguir el motivo -- el usuario se entera igual de que su cuenta ya no está disponible, sin importar si el cierre lo pidió él mismo o si PanAccess lo dio de baja por su cuenta. Si en algún momento se prefiere diferenciar el mensaje según el motivo (por ejemplo, uno más genérico para el caso automático), es un cambio acotado al contexto que arma cada caller, no a la plantilla en sí.

## Qué queda fuera de este cambio (evaluado, no resuelto acá)

- El aviso en tiempo real a dispositivos vinculados (WebSocket) sigue siendo "mejor esfuerzo": un dispositivo apagado en el momento de la revocación no recibe nada hasta que intenta reconectarse, momento en el que el sistema lo rechaza por el estado guardado (`DeviceSession.status`). No hay cola ni reintento de ese aviso puntual.
- No se confirmó si otros endpoints (streaming, API REST del dispositivo, fuera del consumer de WebSocket) también revisan `DeviceSession.status` en cada request -- quedó señalado como punto a verificar aparte, no cubierto por este cambio.

## Verificación hecha

- `python3 -m py_compile` sobre los 5 archivos modificados/creados -- sin errores.
- `python manage.py check` -- "System check identified no issues".
- `manage.py shell`: se importaron todos los módulos tocados sin error de import circular, y se renderizaron las plantillas nuevas con un contexto de prueba (texto y HTML generados correctamente).
- **No se pudo probar contra una base de datos real** -- este entorno de trabajo no tiene conexión a Postgres (`connection to server at "localhost" ... Connection refused`). Antes de confiar en esto en producción, conviene correr una sincronización de prueba (o invocar `_delete_local_subscribers_not_in_remote` a mano con un código de prueba) contra un entorno con base de datos real, y confirmar que el correo efectivamente llega y que `devices_revoked` refleja conteos correctos.
