# Plan: exigir la contraseña actual para cambiarla (Alto #6)

Fecha: 2026-08-26
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Alto #6
Estado: **Plan, sin implementar todavía.**

## Alcance corregido (recapitulando)

El hallazgo original mencionaba dj-rest-auth y `REST_AUTH`, pero esa vista nativa no está montada -- no aplica. El endpoint real que hay que tocar es:

- **`POST /api/v1/profile/password/`** → `profile_password_view` (`wind/api/profile/views.py:67`), con `ProfilePasswordSerializer` (`wind/api/profile/serializers.py:42`). Es el único endpoint de cambio de contraseña autenticado que sigue montado y en uso -- confirmado el contrato documentado para todos los clientes (web + apps) en `docs/GUIA_INTEGRACION_UNIFICADA.md`.
- El otro candidato, `change_password_view` (`wind/functions/change_password.py`), **ya no tiene ruta activa** (se dio de baja el 2026-08-25 junto con el resto de rutas nativas, ver `docs/LIMPIEZA_RUTAS_AUTH_NATIVAS_2026-08-25.md`) -- queda la función en el archivo pero no es alcanzable por HTTP. No hace falta tocarlo.
- La invalidación de sesiones (JWT + dispositivos vinculados) **ya está resuelta** desde antes (`sync_password_locally()` llama `mark_password_changed()` y `revoke_all_device_sessions_for_subscriber()`) -- no es parte de este plan.

Lo único que falta: **`profile_password_view` no exige la contraseña actual**. Body de hoy: `{"code", "newPass"}`. Cualquiera con un JWT válido (robado, filtrado, o una sesión abierta en un dispositivo que el usuario ya no controla) puede cambiar la contraseña sin volver a probar que conoce la actual -- y, como el cambio de contraseña además invalida el resto de sesiones, esto se puede usar para **expulsar al dueño real de la cuenta** con solo un JWT robado, sin necesidad de la contraseña.

## Diseño propuesto

### 1. Nuevo campo `oldPass` (obligatorio) en `ProfilePasswordSerializer`

```python
class ProfilePasswordSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
    oldPass = serializers.CharField(max_length=255, write_only=True)
    newPass = serializers.CharField(max_length=255, write_only=True)
    ...
```

### 2. Verificación reutilizando la misma lógica de login

`wind.services.subscriber_auth.verify_panaccess_credentials(login, password)` ya hace exactamente esta comprobación (cache local primero, PanAccess en vivo si hace falta, con la caché de descubrimiento de Fase 2 ya en su lugar) -- se reutiliza tal cual, pasando `code` como `login`:

```python
from wind.services.subscriber_auth import verify_panaccess_credentials

record = verify_panaccess_credentials(code, old_pass)
if not record:
    return Response(
        {"success": False, "code": "old_password_incorrect", "message": "La contraseña actual no es correcta."},
        status=status.HTTP_400_BAD_REQUEST,
    )
```

No hay que reinventar ninguna comparación de hash -- es el mismo camino ya auditado que usa `authenticate_portal_user()`.

### 3. Throttle/lockout específico para intentos fallidos de `oldPass`

`ProfileThrottle` (120/min por usuario) ya limita el endpoint completo, pero no distingue "cambié mi contraseña 5 veces hoy" de "alguien con mi JWT está probando mi contraseña actual a ciegas". Mismo patrón ya usado en el login (Alto #5, Fase 3): un contador en caché (Redis) por `subscriber_code`, que bloquea el endpoint unos minutos tras varios `oldPass` incorrectos seguidos -- reutilizando `AuthLockoutConfig` o una variante propia (`ProfilePasswordLockoutConfig`) con umbrales independientes, ya que acá el atacante necesita un JWT válido de entrada (barrera más alta que el login anónimo), así que probablemente amerite un umbral más laxo que el de login.

### 4. Respuesta y códigos de error

- `oldPass` incorrecta → `400`, `code: "old_password_incorrect"` (nuevo, estable, para que el cliente muestre un mensaje específico en vez de un error genérico).
- `oldPass` faltante → `400` de validación normal del serializer (ya lo maneja DRF).
- Bloqueado por intentos fallidos → `429`, mismo patrón que el resto de throttles.
- Todo lo demás (política de contraseña, errores de PanAccess) sigue igual que hoy.

## Impacto en clientes -- esto también es un cambio de contrato

Igual que el Alto #3, esto **no se puede activar de un lado sin coordinar con los otros**: `oldPass` pasa a ser un campo obligatorio nuevo en un endpoint que ya está en producción.

- **Web:** el formulario de "Contraseña" en `dashboard.html` (el que se le acaba de agregar el toggle mostrar/ocultar) solo tiene "Nueva contraseña" y "Confirmar contraseña" -- hay que agregar un tercer campo "Contraseña actual" antes de esos dos, con el mismo componente de toggle ya disponible (`app-input-wrap` / `password-toggle.js`), y mandar `oldPass` en el body de `/api/v1/profile/password/`.
- **Android / iOS:** confirmar si tienen una pantalla propia de "cambiar contraseña" (fuera del dashboard web) que llame a este mismo endpoint -- si es así, necesitan el mismo campo nuevo antes de que el backend lo exija.
- **Rollout sugerido** (para no romper clientes que todavía no actualizaron):
  1. Desplegar el backend aceptando `oldPass` como **opcional** primero: si viene, se valida contra la actual (falla si no coincide); si no viene, se deja pasar igual que hoy pero se loguea una advertencia (`DEPRECATION: cambio de contraseña sin oldPass para %s`) para medir cuántos clientes siguen sin mandarlo.
  2. Una vez que Web/Android/iOS confirmen que ya mandan `oldPass` en producción (revisando esas métricas de log), flipear a **obligatorio** de verdad -- ahí sí, cualquier request sin `oldPass` se rechaza con 400.
  3. Esto evita coordinar un despliegue simultáneo exacto entre 3 equipos distintos, a costa de dejar la ventana de la vulnerabilidad abierta un poco más mientras dura la fase 1. Alternativa más estricta: coordinar un corte simultáneo y saltar directo al paso 2 -- a decidir según qué tan rápido puedan moverse los 3 equipos.

## Qué NO cambia

- No se toca `reset_password_in_panaccess`/`sync_password_locally` -- la invalidación de JWT y revocación de dispositivos sigue funcionando igual, ahora simplemente protegida por la verificación de `oldPass` antes de llegar ahí.
- No afecta el flujo de "olvidé mi contraseña" (`password_reset.py`) -- ese flujo es intencionalmente sin `oldPass` (todo el punto es que el usuario no la recuerda), y ya tiene su propia protección (token firmado + de un solo uso).

## Pasos de implementación (cuando se dé luz verde)

1. `wind/api/profile/serializers.py`: agregar `oldPass` a `ProfilePasswordSerializer` (opcional en la fase 1 del rollout, ver arriba).
2. `wind/api/profile/views.py::profile_password_view`: llamar `verify_panaccess_credentials(code, old_pass)` antes de `reset_password_in_panaccess`; nuevo branch de respuesta `400`/`old_password_incorrect`.
3. Lockout por intentos fallidos: nueva config en `appConfig.py` (+ `.env`), reutilizando el patrón cache-based de Fase 3 del login.
4. `appConfig.py`/`.env`: sin nuevas variables de PanAccess -- solo las de lockout si se implementa ese punto.
5. Verificación: `py_compile` + `manage.py check` (sin DB), y luego pruebas contra producción (cambiar contraseña con `oldPass` correcta/incorrecta/ausente) igual que el resto de cambios de este ciclo.
6. Documentar en `docs/VERIFICACION_CONTRASENA_ACTUAL_2026-08-26.md` (o el nombre que corresponda a la fecha real de implementación) y actualizar la fila del Alto #6 en `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`.
7. Avisar a Web/Android/iOS con el contrato nuevo (mismo estilo que `docs/INTEGRACION_APROVISIONAMIENTO_HIBRIDO_APPS_2026-08-26.md`) antes de pasar `oldPass` a obligatorio.

## Abierto para decidir antes de implementar

- ¿Rollout en 2 fases (opcional → obligatorio) o corte simultáneo coordinado? Afecta cuánto tarda en cerrarse la vulnerabilidad real.
- Umbral/duración del lockout por `oldPass` incorrecta -- ¿mismo que login (5 intentos / 15 min) o más permisivo, dado que ya requiere un JWT válido?
- ¿Confirmar con Android/iOS si de verdad tienen una pantalla de cambio de contraseña propia, o si en la práctica solo la web la usa? Cambia si hace falta coordinación real con esos 2 equipos o no.
