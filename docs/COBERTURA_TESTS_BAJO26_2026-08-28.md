# Cobertura de tests — Bajo #26

Fecha: 2026-08-28

## Qué

Hallazgo Bajo #26: "Huecos de cobertura de test en la integración PanAccess, login social, invalidación de JWT y device session." Se verificó con grep sobre `wind/tests/` qué de eso tenía cero cobertura real, y se escribieron tests para los tres huecos confirmados (la integración PanAccess general ya tenía cobertura razonable vía `test_auth.py`, `test_get_smartcard.py`, `test_panaccess_deprovision.py`, etc.).

Archivos nuevos:

- `wind/tests/test_device_session.py` (8 tests) -- `DeviceSession.revoke()`, generación de `device_token`, y la tarea `expire_idle_device_sessions_task` (Bajo #28): revoca solo sesiones activas con `last_seen_at` más viejo que el umbral configurado, no toca sesiones recientes ni ya revocadas, respeta el borde exacto del umbral, y no hace nada si `DEVICE_SESSION_IDLE_EXPIRY_ENABLED=false`.
- `wind/tests/test_jwt_password_invalidation.py` (8 tests) -- `PasswordAwareJWTAuthentication.get_user()`: acepta cuando el usuario no tiene `UserSecurityProfile`, rechaza un access token con `iat` anterior al cambio de contraseña, acepta uno posterior, y no rompe si falta el claim `iat`. `mark_password_changed()`: actualiza el timestamp, blacklistea los refresh tokens vigentes (`OutstandingToken`/`BlacklistedToken`), y un test extremo a extremo confirma que un access token viejo queda rechazado inmediatamente después de llamarlo (sin esperar a que expire por su cuenta).
- `wind/tests/test_social_login.py` (11 tests) -- `PanAccessSocialAccountAdapter.pre_social_login()`: rechaza sin email, rechaza si el proveedor no confirma verificación (ni por `email_addresses` ni por el fallback `extra_data.email_verified`), fusiona con un `User` local existente por email, propaga el `provider` real (no un default fijo) a `ensure_subscriber_for_social_email`, y traduce `SocialLoginSubscriberNotFound` a un mensaje específico. `ensure_subscriber_for_social_email()`: reutiliza el registro existente sin tocar PanAccess, vincula desde `ListOfSubscriber` si ya existe ahí, respeta la bandera `SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER`, y auto-registra (marcando `is_social_account=True` y el `social_provider` correcto) cuando la bandera está apagada.

Los tests de login social usan un doble de prueba (`SimpleNamespace`) para `SocialLogin` en vez de levantar allauth/OAuth completo -- el adaptador solo toca `.user`, `.account.extra_data`, `.account.provider` y `.email_addresses`, así que no hace falta más que eso para ejercer la lógica propia del proyecto.

## Por qué

Estas tres piezas son justamente las que más dependen de "que nadie las rompa sin darse cuenta": la expiración de `DeviceSession` corre sola en un Celery beat diario sin que nadie la mire; la invalidación de JWT es el mecanismo que cierra la ventana de "token robado sigue sirviendo después de cambiar la contraseña" (une dos módulos -- `password_reset.py` y `jwt_invalidation.py` -- fácil de desalinear en un refactor futuro); y el login social tiene la lógica más sutil de las tres (verificación de proveedor para evitar account takeover, fusión de usuarios, prefijo de código por proveedor) sin ningún test que la fije.

## Cómo se verificó

Como este sandbox no tenía PostgreSQL disponible (la única causa de que `manage.py test` fallara al inicio: "Connection refused" a `localhost:5432`), se levantó una instancia local de Postgres dentro del propio sandbox (paquete `pgserver`, binarios embebidos, sin tocar nada de infraestructura real) con las mismas credenciales que ya usa `.env` (`DB_NAME=wind_db`, `DB_USER=wind_user`, mismo `DB_PASSWORD`), para poder correr los tests contra una base real y no solo `py_compile`. Nunca se tocó el `.env` ni ningún servidor de producción -- la base es efímera y vive solo dentro de este sandbox.

Con esa base:
- Los 3 archivos nuevos: **23/23 tests OK**.
- Suite completa (`wind` + `telemetry`, 77 tests) para confirmar que la limpieza de código muerto (Bajo #25) no rompió nada: **69 OK, 2 failures + 6 errors**, pero los 8 casos fallidos se re-corrieron en aislamiento y **fallan igual sin ninguno de los cambios de esta sesión** (ver "Hallazgo colateral" abajo) -- ninguno toca archivos editados en Bajo #25/#26.

## Hallazgo colateral (no corregido, fuera del alcance de esta tarea)

Al correr la suite completa aparecieron 3 tests preexistentes que fallan de forma independiente a cualquier cambio de esta sesión (confirmado corriéndolos solos, en una base de test nueva):

- `wind.tests.test_auth.ClosedSubscriberLoginTestCase.test_get_or_create_portal_user_does_not_reactivate_closed_subscriber` -- `assertFalse(user.is_active)` falla (`True is not false`).
- `wind.tests.test_subscriber_trial.SubscriberTrialEligibilityTestCase.test_new_email_is_eligible` -- `DatabaseOperationForbidden`: la clase no hereda de `TestCase`/`TransactionTestCase` (aparenta ser `SimpleTestCase`), pero el código bajo prueba sí hace una query real.
- `wind.tests.test_password_reset.PasswordResetServiceTestCase` (varios casos) -- `InterfaceError: connection already closed` en `setUp()`.

Estos tres no se tocaron ni se investigaron a fondo -- quedan señalados para una sesión aparte si se decide perseguirlos, ya que no son parte de los huecos de cobertura de Bajo #26 (son tests que ya existían y que, aparentemente, ya estaban rotos).

## Archivos tocados

- `wind/tests/test_device_session.py` (nuevo)
- `wind/tests/test_jwt_password_invalidation.py` (nuevo)
- `wind/tests/test_social_login.py` (nuevo)
- `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (fila Bajo #26 actualizada)
