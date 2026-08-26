# Verificación de contraseña actual al cambiarla (Alto #6)

Fecha: 2026-08-26
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (Alto #6), `docs/PLAN_VERIFICACION_CONTRASENA_ACTUAL_2026-08-26.md` (plan original)
Estado: **Implementada la fase 1 del rollout (backend acepta `oldPass` opcional; web ya lo manda). Falta confirmar Android/iOS y, más adelante, volverlo obligatorio.**

## De qué se trata

`POST /api/v1/profile/password/` (`profile_password_view`) cambiaba la contraseña de un suscriptor solo con `code` + `newPass` -- sin pedir la contraseña actual. Como el cambio de contraseña además invalida el resto de sesiones JWT y revoca dispositivos vinculados (ya resuelto en Alto #4), esto significa que un JWT robado o filtrado alcanzaba, por sí solo, para tomar la cuenta y expulsar al dueño real, sin necesidad de conocer la contraseña.

## Qué se implementó

### Backend

- **`wind/api/profile/serializers.py`**: `ProfilePasswordSerializer` ahora acepta `oldPass` (opcional por ahora, fase 1 del rollout).
- **`wind/api/profile/views.py::profile_password_view`**:
  - Si `oldPass` viene en el body, se verifica contra la contraseña actual real reutilizando `verify_panaccess_credentials(code, old_pass)` de `wind/services/subscriber_auth.py` -- el mismo camino ya auditado que usa el login manual (cache local primero, PanAccess en vivo si hace falta, con la caché de descubrimiento de la Fase 2 del Alto #5 ya en su lugar). Si no coincide, `400` con `code: "old_password_incorrect"`.
  - Si `oldPass` no viene, el cambio se aplica igual que antes (compatibilidad), pero queda un log `DEPRECATION: cambio de contraseña sin oldPass para %s` para medir cuántos clientes todavía no lo mandan antes de volverlo obligatorio.
  - Bloqueo temporal (mismo patrón cache-based que el lockout de login, Alto #5 Fase 3): 5 intentos fallidos de `oldPass` en 5 minutos bloquean el endpoint para ese `subscriber_code` por 15 minutos (`429`, `code: "old_password_locked"`). Umbral independiente del de login (`ProfilePasswordLockoutConfig`), porque acá el atacante ya necesita un JWT válido -- barrera más alta que el login anónimo.
- **`appConfig.py`**: nueva clase `ProfilePasswordLockoutConfig` (`ENABLED`/`MAX_ATTEMPTS`/`WINDOW_SECONDS`/`LOCKOUT_SECONDS`, envs `PROFILE_PASSWORD_LOCKOUT_*`, mismos defaults que el lockout de login).
- **`.env`**: `PROFILE_PASSWORD_LOCKOUT_ENABLED=True`, `PROFILE_PASSWORD_LOCKOUT_MAX_ATTEMPTS=5`, `PROFILE_PASSWORD_LOCKOUT_WINDOW_SECONDS=300`, `PROFILE_PASSWORD_LOCKOUT_DURATION_SECONDS=900`.

### Web (`wind/templates/wind/dashboard.html`)

- Nuevo campo "Contraseña actual" en el formulario de cambio de contraseña (pestaña "Contraseña"), antes de "Nueva contraseña" -- mismo componente de toggle mostrar/ocultar agregado en el cambio anterior (`password-toggle.js`).
- El JS ya manda `oldPass` en el body de `POST /api/v1/profile/password/`, junto con `code`/`newPass`. Validación local: si el campo está vacío, no se abre el modal de confirmación (mensaje "Ingresa tu contraseña actual.").

### Android / iOS

**No implementado en este cambio** -- este backend/sesión no tiene acceso a esos repositorios. Sigue pendiente confirmar con esos equipos si tienen una pantalla propia de "cambiar contraseña" que llame a este mismo endpoint (el plan original lo dejaba como pregunta abierta). Mientras no se confirme, el backend sigue aceptando el cambio sin `oldPass` (fase 1), así que ningún cliente existente se rompe por este despliegue.

## Decisión tomada sobre el rollout

El plan dejaba dos opciones abiertas. Se implementó la **opción de 2 fases** (más conservadora):

1. **Fase 1 (esta implementación):** `oldPass` opcional. Si un cliente ya lo manda (como la web, a partir de ahora), se verifica de verdad. Si no lo manda todavía (posibles apps sin actualizar), el cambio se sigue permitiendo, con el log de advertencia para medir la migración.
2. **Fase 2 (pendiente, requiere decisión explícita más adelante):** una vez confirmado -- revisando el log `DEPRECATION: cambio de contraseña sin oldPass` en producción, y/o confirmación directa de Android/iOS -- que todos los clientes ya mandan `oldPass`, cambiar `required=False` a `required=True` en el serializer. Ahí sí, cualquier request sin `oldPass` se rechaza con `400`, cerrando la ventana de la vulnerabilidad por completo.

## Qué NO cambió

- `reset_password_in_panaccess`/`sync_password_locally` siguen igual -- la invalidación de JWT y revocación de dispositivos (Alto #4) sigue funcionando exactamente como antes, ahora protegida por la verificación de `oldPass` cuando el cliente la manda.
- El flujo de "olvidé mi contraseña" no se tocó -- sigue sin pedir contraseña anterior (por diseño, ya tiene su propia protección con token firmado de un solo uso).
- `change_password_view` (`wind/functions/change_password.py`) no se tocó -- confirmado que no tiene ruta activa desde la limpieza de rutas nativas del 2026-08-25, no es alcanzable por HTTP.

## Verificación hecha

- `python3 -m py_compile` sobre `wind/api/profile/serializers.py`, `wind/api/profile/views.py`, `appConfig.py` -- sin errores.
- `python3 manage.py check` -- "System check identified no issues".
- `manage.py shell`: confirmado que `ProfilePasswordLockoutConfig` resuelve a los defaults esperados, y que `ProfilePasswordSerializer` es válido tanto con `oldPass` como sin él (fase 1, compatibilidad confirmada).
- `dashboard.html` carga sin errores de sintaxis Django, y los `<div>` nuevos quedan balanceados.
- **Pendiente (requiere producción):** probar el flujo real -- cambiar contraseña con `oldPass` correcta (éxito), incorrecta (`400`/`old_password_incorrect`), ausente (funciona igual que antes, con el log de advertencia), y 5 intentos fallidos seguidos (`429`/`old_password_locked`, y que se libere solo tras los 15 minutos).

## Cómo desplegar y verificar en producción

1. `git pull` en `/opt/panaccess-wind` -- sin migración nueva (todo cache-based).
2. Confirmar que las 4 variables `PROFILE_PASSWORD_LOCKOUT_*` quedaron en `.env`.
3. Reiniciar los 8 procesos Daphne, y correr `collectstatic` si no se hizo ya para el cambio anterior del toggle mostrar/ocultar (este cambio no agrega JS nuevo, pero `dashboard.html` sí cambió).
4. Prueba segura sugerida: desde el dashboard web, cambiar la contraseña de una cuenta de prueba con la contraseña actual correcta (debe funcionar igual que siempre), después con una incorrecta a propósito (debe rechazar con el mensaje "La contraseña actual no es correcta."), y repetir el error 5 veces seguidas para confirmar el bloqueo temporal.
5. Revisar en los logs cuántas veces aparece `DEPRECATION: cambio de contraseña sin oldPass` en los primeros días -- si aparece solo por pruebas manuales viejas o no aparece, es señal de que se puede pasar a la fase 2 (obligatorio) sin romper nada.
