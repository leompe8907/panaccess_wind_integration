# Expiración por inactividad de "dispositivos vinculados" (Bajo #28)

Fecha: 2026-08-28
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Bajo #28.

## Problema

`DeviceSession` (`wind/models.py:747-786`, el modelo detrás del panel "dispositivos vinculados" del dashboard) no tenía ningún mecanismo de expiración propio. Una sesión de dispositivo quedaba `active` para siempre salvo que pasara una de dos cosas: el usuario la revocaba a mano desde el dashboard, o cambiaba su contraseña (`revoke_all_device_sessions_for_subscriber`, Alto #4/#6). Fuera de esos dos casos puntuales, un `device_token` seguía siendo válido para autenticarse contra `/ws/device/` sin límite de tiempo, sin importar cuánto hacía que ese dispositivo no se conectaba de verdad.

Riesgo concreto: un celular vendido/perdido, una Smart TV reseteada de fábrica sin cerrar sesión antes, o simplemente un dispositivo que el usuario dejó de usar y se olvidó que tenía vinculado, seguían siendo dispositivos "de confianza" indefinidamente. El único mecanismo de poda (cambio de contraseña) no cubre el caso más común, que es simplemente dejar de usar un dispositivo sin que pase ningún evento de seguridad que dispare una revocación.

## Solución implementada

Expiración automática por inactividad: una tarea periódica de Celery revoca cualquier `DeviceSession` cuyo `last_seen_at` supere el umbral configurado (183 días). No hizo falta tocar `device_consumers.py` -- ya rechazaba cualquier reconexión con `status != "active"` (`device_token_invalid`, ver `wind/device_consumers.py:91`), así que alcanza con marcar `revoked` desde la tarea de fondo para que el resto del sistema ya lo trate como inválido.

### Cambios de código

1. **`appConfig.py`** (`CeleryConfig`): 3 variables nuevas --
   - `DEVICE_SESSION_IDLE_EXPIRY_ENABLED` (bool, default `True`)
   - `DEVICE_SESSION_IDLE_EXPIRY_DAYS` (default **183**)
   - `DEVICE_SESSION_CLEANUP_MINUTES` (cada cuánto corre la tarea, default 1440 = una vez al día)
2. **`.env`**: `CELERY_DEVICE_SESSION_IDLE_EXPIRY_ENABLED=True`, `DEVICE_SESSION_IDLE_EXPIRY_DAYS=183`, `CELERY_DEVICE_SESSION_CLEANUP_MINUTES=1440`.
3. **`wind/tasks.py`**: nueva tarea `expire_idle_device_sessions_task` -- un solo `UPDATE` en bloque (`DeviceSession.objects.filter(status="active", last_seen_at__lt=cutoff).update(status="revoked", revoked_at=now(), revoked_reason="idle_timeout")`), sin traer filas a Python ni iterar (a esta escala no hace falta log caso por caso).
4. **`panaccess_wind_integration/settings.py`**: entrada nueva `expire-idle-device-sessions` en `CELERY_BEAT_SCHEDULE`, mismo patrón que `retry-partial-closures`/`recover-pending-audit-logs` (cola `_PIPELINE_QUEUE`, `expires` igual al intervalo).

### Por qué 183 días y no menos

Es un valor deliberadamente conservador (medio año) para no arriesgar sesionar dispositivos que el usuario sigue usando pero con poca frecuencia (ej. una Smart TV que se prende una vez cada tanto). Cualquier dispositivo real que siga en uso refresca `last_seen_at` en cada reconexión (`device_consumers.py`, `_register_or_refresh_device`), así que nunca se acerca al umbral mientras esté activo -- el corte solo afecta a lo que realmente lleva medio año sin conectarse. Es ajustable por `.env` sin tocar código si en el futuro se quiere un valor distinto.

## Verificación realizada

- `py_compile` sobre `appConfig.py`, `wind/tasks.py`, `panaccess_wind_integration/settings.py`: sin errores.
- `manage.py check`: `System check identified no issues (0 silenced)`.
- Pendiente (cuando se despliegue): confirmar que Celery Beat toma el nuevo schedule (reiniciar el proceso beat) y, opcionalmente, correr la tarea una vez a mano (`expire_idle_device_sessions_task.delay()` o vía shell) contra producción para confirmar el conteo de `revoked` antes de esperar al primer disparo automático.

## Nota aparte: Medio #19 ya estaba resuelto

Durante esta revisión se confirmó que el hallazgo Medio #19 (`pre_social_login` no revisaba `email_verified` del proveedor social) **ya está resuelto en el código actual** -- `wind/adapters.py:53-97` (`_is_email_verified_by_provider` + el `raise ValidationError` si no está verificado) ya lo implementa correctamente. No se encontró en qué sesión se hizo este cambio puntual, pero está confirmado en el código en producción. Se corrige la tabla de auditoría para reflejarlo.

## Estado

Bajo #28 queda **resuelto**. Medio #19 se corrige en la tabla como ya resuelto (hallazgo obsoleto, sin acción pendiente).
