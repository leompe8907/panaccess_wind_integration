# IP del causante en errores de backend (`platform=backend`)

Fecha: 2026-09-10.

## Qué pidió el cliente

Tras ver un issue real en el panel de diagnóstico (`Plataforma: backend`, "Login fallido: There was a DB conflict with another user's action that could not be resolved by retrying once...", 50 ocurrencias), preguntó si se le podía agregar la IP de quien disparó cada error.

## Qué había antes

`LogEvent.client_ip` (`applogs/models.py`) ya existía en el modelo desde el diseño original (docs/LOGS_DIAGNOSTICO_2026-09-01.md), y el endpoint HTTP de ingesta para apps cliente (`applogs/views.py::ingest_log_view`) ya lo poblaba con `get_client_ip(request)`. El hueco estaba específicamente en la otra mitad del sistema: `applogs/logging_handler.py::DiagnosticsLogHandler`, el handler que engancha los `ERROR`/`WARNING` del propio backend (loggers `django`, `wind`, `wind.utils.panaccess_auth`, `wind.services.panaccess_singleton`, `celery*`, etc. -- ver `LOGGING` en `settings.py`) al mismo pipeline de diagnóstico. Ese handler solo recibe un `logging.LogRecord` plano -- sin ningún vínculo al `request` que disparó ese `logger.error(...)`, muchas veces varias capas por debajo de cualquier vista (el caso concreto: `wind/utils/panaccess_auth.py::login()`, llamado por `wind/services/panaccess_singleton.py` cada vez que CUALQUIER request necesita hablar con PanAccess). Resultado: para `platform=backend`, `client_ip` quedaba siempre `NULL`.

## Origen real del mensaje del ejemplo (aclaración, no es un bug de este repo)

El texto "There was a DB conflict with another user's action that could not be resolved by retrying once" no lo genera este código -- es el `errorMessage` que devuelve la propia API de PanAccess (sistema externo de condicional access) cuando el login de servicio del backend contra PanAccess falla. `panaccess_singleton.py::_authenticate_with_retry()` ya reintenta esto con backoff (hasta 5 veces) y alerta después de 3 fallos seguidos -- este trabajo no cambia esa lógica, solo hace que el issue que ya se registraba en el panel de diagnóstico quede además con la IP de la request que estaba en curso cuando ocurrió.

## Qué se implementó

**`wind/utils/request_context.py`** (nuevo): un `contextvars.ContextVar` que guarda la IP "actual" -- `set_current_client_ip()`, `reset_current_client_ip()`, `get_current_client_ip()`. Se eligió `contextvars` en vez de un threadlocal clásico porque es seguro tanto en WSGI sync (un thread por request) como en el ASGI async de Channels (cada conexión corre en su propia Task, y `asgiref.sync_to_async`/`database_sync_to_async` propagan el contexto al thread pool que usan por debajo).

**`wind/middleware/request_context_middleware.py`** (nuevo): `RequestContextMiddleware` fija la IP (reusando `wind.utils.websocket_utils.get_client_ip()`, el mismo resolutor con trusted-proxy que ya usa el resto del código) al entrar cada request HTTP, y la restaura al salir (con `try/finally`, incluso si la vista lanza). Instalado siempre en `MIDDLEWARE` (`settings.py`), justo después de `SecurityMiddleware` -- sin flag, porque el costo es un `get_client_ip()` ya optimizado por request y no cambia ningún comportamiento existente.

**`applogs/logging_handler.py`**: `DiagnosticsLogHandler._emit()` ahora pasa `client_ip=get_current_client_ip()` a `record_log_event()`.

**`wind/consumers.py` y `wind/device_consumers.py`** (`/ws/auth/` y `/ws/device/`): mismo mecanismo, fijado en `connect()` (con `get_client_ip_from_scope(self.scope)`, el resolutor equivalente para ASGI) y restaurado en `disconnect()` -- para que un error de backend disparado durante el ciclo de vida de una conexión WebSocket (ej. el mismo login de servicio a PanAccess, disparado esta vez por un pareo de TV) también quede asociado a la IP de esa conexión.

## Qué significa para el panel de diagnóstico

De acá en adelante, cada `LogEvent` nuevo de `platform=backend` trae `client_ip` poblado si el error ocurrió durante una request HTTP o una conexión WebSocket -- queda `null` únicamente si el error ocurrió fuera de cualquiera de los dos (ej. una tarea Celery en background, sin ningún request de por medio, correctamente sin IP que reportar). Los `LogEvent` ya existentes (los 50 del ejemplo que disparó este pedido) no se pueden completar retroactivamente -- el campo no se puede reconstruir después de los hechos.

## Cómo se verificó

`py_compile` sobre los 6 archivos tocados, `manage.py check` sin issues, y una corrida real contra Postgres (harness efímero con `pgserver`, mismo patrón usado en el resto de esta sesión):

- `applogs` completo (28 tests, incluidos 6 nuevos: contextvar set/reset, middleware set/reset incluso si la vista lanza, y el handler capturando/dejando `null` la IP según haya o no contexto) -- **OK**.
- Suite de pareo de TV/dispositivos vinculados (`test_websocket`, `test_device_session`, `test_link_device_view`, `test_udid_account_association`, `test_udid_request_ip_rate_limit`, `test_go_windtv_view` -- 26 tests, ejercitan directamente los dos consumers tocados) -- **OK**.
- `test_password_reset` + `test_account_deletion_scheduled` (37 tests, para confirmar que agregar un middleware nuevo a `MIDDLEWARE` -- que corre en TODAS las requests -- no rompió nada en otros flujos ya cubiertos) -- **OK**.

Total: 91 tests corridos para esta verificación, sin fallas.
