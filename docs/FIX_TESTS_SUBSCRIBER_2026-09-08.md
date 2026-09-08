# Fixes a 3 problemas encontrados corriendo la suite de tests completa

Fecha: 2026-09-08
Contexto: al correr `wind` completo (133 tests) contra Postgres real local, salieron 2 FAIL + 3 ERROR. 3 de los ERROR ya se sabían (falta de Redis en el sandbox de diagnóstico, no aplica corriendo local con Redis real). Quedaban 2 FAIL + 3 ERROR reales, cubiertos acá.

## 1. Bug real: reactivaba el User de un abonado cerrado

`wind/services/subscriber_auth.py::get_or_create_portal_user()`.

**Antes:**
```python
if not is_subscriber_closed_locally(code):
    user.is_active = True
user.save()
```
Si el abonado está cerrado, esta rama simplemente no tocaba `is_active` -- pero nunca lo ponía en `False` tampoco. Para un `User` recién creado con `create_user()` (default del modelo `is_active=True`), eso dejaba la cuenta **activa** igual, aunque el abonado esté `CLOSED`. El comentario decía "segunda capa de protección" pero en la práctica no protegía nada para usuarios nuevos.

**Ahora:**
```python
if is_subscriber_closed_locally(code):
    user.is_active = False
else:
    user.is_active = True
user.save()
```
Fuerza el estado explícito en los dos sentidos.

Detectado por: `wind.tests.test_auth.ClosedSubscriberLoginTestCase.test_get_or_create_portal_user_does_not_reactivate_closed_subscriber` (`AssertionError: True is not false`).

Este es el que más importa: es autenticación real (auditoría sección 17/21, ver docstring de la clase), no un problema de test.

## 2. Bug de test (no de negocio): `test_new_email_is_eligible`

`wind/tests/test_subscriber_trial.py`. A este método le faltaba el `@patch("...SubscriberEmailRegistry.objects.filter")` que sí tienen sus 2 hermanos en la misma clase (`SimpleTestCase`, no permite queries reales). Sin el mock, pegaba contra la BD real y Django lo rechazaba con `DatabaseOperationForbidden`. Se agregó el mismo mock, devolviendo `first()=None` (simula "email nuevo, sin registro previo"), que es justo lo que el nombre del test dice que prueba.

## 3. Bug de infraestructura de test: Celery rompía las transacciones de `TestCase`

`panaccess_wind_integration/celery.py`. Los hooks `task_prerun`/`task_postrun` llaman `close_old_connections()` -- correcto y necesario en producción, para que Celery Beat no se quede con una conexión de Postgres muerta corriendo horas sin parar (ver comentario original en el archivo).

El problema: con `CELERY_TASK_ALWAYS_EAGER=True` (activo en tests), una tarea de Celery corre sincrónica en el mismo proceso que el test. Si un test dispara una tarea a mitad de un `TestCase` (p. ej. el envío de email en `test_confirm_password_reset_listofsubscriber_fallback`), estos hooks cerraban la conexión de Postgres **en medio de la transacción atómica** que Django usa para aislar cada test -- rompiendo con `psycopg2.InterfaceError: connection already closed` los tests que corrían después en la misma clase (`PasswordResetServiceTestCase`: 3 tests afectados).

Fix: mismo patrón que ya usa `settings.py` (`if 'test' in sys.argv`) para detectar `manage.py test` y saltarse el `close_old_connections()` solo en ese caso. En producción `sys.argv` nunca contiene `'test'`, así que el comportamiento real no cambia en nada.

## Verificación

Corrido en sandbox (Postgres real vía `pgserver`, sin Redis) el subset afectado: `wind.tests.test_auth`, `wind.tests.test_password_reset`, `wind.tests.test_subscriber_trial` -- 25 tests, 0 FAIL, solo los 3 ERROR ya conocidos de `SubscriberRegistrationTestCase` por falta de Redis en ese sandbox (no reproducen local, donde sí hay Redis).

Pendiente de confirmación final: correr `deploy\run_tests_local.bat` completo (133 tests) en tu máquina, donde ya corriste la suite completa antes y sí tenés Redis real.

## Archivos tocados

- `wind/services/subscriber_auth.py` (fix de negocio real)
- `wind/tests/test_subscriber_trial.py` (fix de test)
- `panaccess_wind_integration/celery.py` (fix de test/infraestructura, sin impacto en producción)
