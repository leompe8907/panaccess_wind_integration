# Alto #6 — Fase 2: `oldPass` obligatorio (2026-08-28)

Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (Alto #6), `docs/PLAN_VERIFICACION_CONTRASENA_ACTUAL_2026-08-26.md` (diseño original), `docs/VERIFICACION_CONTRASENA_ACTUAL_2026-08-26.md` (fase 1, verificada en producción).

## Qué cambió

Desde la fase 1 (2026-08-26), `POST /api/v1/profile/password/` aceptaba `oldPass` como campo **opcional**: si venía, se verificaba contra la contraseña real; si no venía, el cambio se dejaba pasar igual (solo quedaba un log `DEPRECATION` para medir adopción). Esa fase intermedia existía para no romper clientes que todavía no mandaban el campo.

Confirmado con el cliente que **Web, Android e iOS ya validan/envían la contraseña actual desde sus propias pantallas** antes de llamar a este endpoint. Con eso confirmado, se cerró la ventana: `oldPass` pasa a ser **obligatorio**.

## Por qué importa que sea obligatorio (y no solo "validado en la app")

La validación que hacen las apps es una conveniencia de UI, no un control de seguridad: quien tenga un JWT válido (robado, filtrado, o de una sesión que el usuario ya no controla) puede llamar al endpoint directamente (`curl`, script, etc.), sin pasar nunca por la pantalla de la app. Mientras `oldPass` fuera opcional, ese ataque seguía funcionando igual que antes de la fase 1 -- bastaba con no mandar el campo. Con `oldPass` obligatorio, el backend mismo exige la prueba de conocer la contraseña actual, cerrando el hueco independientemente de lo que valide o no cada cliente.

## Cambios de código

1. **`wind/api/profile/serializers.py`** (`ProfilePasswordSerializer`): se quitó `required=False` de `oldPass`. Ahora es un `CharField` obligatorio como los demás -- si falta, DRF lo rechaza con `400` antes de llegar a la vista (mismo formato de error que cualquier otro campo requerido faltante).
2. **`wind/api/profile/views.py`** (`profile_password_view`):
   - `old_pass = ser.validated_data["oldPass"]` (antes `.get("oldPass")`, ya que ahora siempre está presente si `ser.is_valid()` pasó).
   - Se eliminó el branch `else` que dejaba pasar el cambio sin `oldPass` y logueaba `DEPRECATION` -- la verificación contra `verify_panaccess_credentials(code, old_pass)` corre siempre, sin condicional.
   - Sin cambios en el lockout (`ProfilePasswordLockoutConfig`, cache-based, 5 intentos / 15 min) ni en los demás códigos de respuesta (`old_password_incorrect`, `old_password_locked`, errores de PanAccess) -- se mantienen igual que en la fase 1.
3. Docstrings/comentarios actualizados en ambos archivos para reflejar que el rollout ya terminó (fase 2, obligatorio) en vez de "fase 1, opcional".

No hubo cambios en `appConfig.py` ni `.env` -- el lockout ya estaba configurado desde la fase 1 y no se tocó ningún umbral.

## Qué NO cambió

- `dashboard.html` ya mandaba `oldPass` desde la fase 1 (campo "Contraseña actual" agregado en ese momento) -- no requirió ningún cambio de frontend.
- La invalidación de JWT/dispositivos (`sync_password_locally` → `mark_password_changed()` / `revoke_all_device_sessions_for_subscriber()`) sigue funcionando igual, ahora simplemente detrás de una verificación de `oldPass` que ya no se puede omitir.
- El flujo de "olvidé mi contraseña" no se toca (sigue sin `oldPass`, por diseño).

## Verificación realizada

- `py_compile` sobre `wind/api/profile/serializers.py` y `wind/api/profile/views.py`: sin errores.
- `manage.py check`: `System check identified no issues (0 silenced)`.
- **Verificado en producción (2026-08-28):** request real a `POST /api/v1/profile/password/` sin `oldPass` → `400` (rechazado por el serializer, no llega a cambiar la contraseña). Confirma que el hueco quedó cerrado -- ya no hay forma de cambiar la contraseña sin probar la actual, ni con un JWT válido a secas.
- Los otros 4 casos (correcta / incorrecta / 5x bloqueo / desbloqueo manual) ya estaban verificados en producción desde la fase 1 y no cambian de comportamiento en la fase 2.

## Estado

Alto #6 queda **completamente resuelto y verificado en producción** (fase 1 + fase 2).
