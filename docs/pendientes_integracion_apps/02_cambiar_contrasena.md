# Cambiar contraseña

Estado: implementado en backend y en appVideo. Contrato estable desde 2026-08-28 (con un cambio de firma que hay que verificar si cada cliente ya adoptó).
Referencia: `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 5.1.

## Contrato

`POST /api/v1/profile/password/` (JWT), body `{"code": "<subscriber_code>", "oldPass": "...", "newPass": "..."}` → `{"success": true, "message": "Contraseña actualizada"}`.

**`oldPass` es obligatorio desde 2026-08-28.** El backend la verifica contra PanAccess antes de aplicar el cambio (evita que un JWT robado alcance por sí solo para cambiar la contraseña). Si algún cliente arma esta pantalla sin pedir la contraseña actual, el backend rechaza con 400 de validación -- no deja pasar el cambio.

**Política de `newPass`:** 8-255 caracteres, al menos una mayúscula, al menos un número, charset `a-z A-Z 0-9 - _` más especiales `! @ # $ % ^ & * ( ) + = [ ] { } ; : ' " , . < > / ? ~ \``. Validar localmente antes de llamar al endpoint para evitar el round-trip en el caso obvio.

**Errores** (siempre con `"success": false` y `"message"`, más `"code"` cuando aplica):

| Status | `code` | Significado |
|---|---|---|
| 400 | `old_password_incorrect` | `oldPass` no coincide con la real -- mostrar mensaje específico. |
| 400 | `password_policy_violation` | `newPass` no cumple la política (detectado localmente). |
| 400 | `password_rejected_by_panaccess` | PanAccess rechazó por una regla no cubierta localmente. Incluye `panaccess_error_code`. |
| 429 | `old_password_locked` | 5 intentos fallidos seguidos de `oldPass` -- bloqueado 15 min. |
| 429 | `rate_limited` | Demasiados intentos en general. |
| 502/503/504 | `panaccess_integration_error`/`panaccess_unavailable`/`panaccess_timeout` | Problema de PanAccess, no del valor enviado. |

## Efecto colateral automático (solo en éxito)

Invalida **todos** los JWT ya emitidos y revoca **todos** los `DeviceSession` de la cuenta -- incluida la propia sesión que hizo el cambio. Esto dispara el mismo push `device_revoked` (razón `"password_changed"`) que una revocación manual -- ver `07_dispositivos_vinculados.md` para el manejo esperado de ese push.

## Estado en appVideo (web)

Implementado: `changePassword()` en `accountSecurityService.js`, UI en `ChangePasswordPanel.jsx`.

## Qué debe implementar iOS/Android nativo

- Mismo endpoint, mismo body (`code`, `oldPass`, `newPass`) -- sin diferencias de plataforma.
- **Pantalla con dos campos, no uno:** si la app todavía solo pedía la contraseña nueva (sin la actual), hay que agregar el campo `oldPass` -- sin él, el backend rechaza el cambio con 400.
- Validar la política de `newPass` localmente antes de llamar, igual que appVideo.
- **Tras un cambio exitoso, la app queda con un JWT y un `device_token` inválidos** (el propio backend los revocó como efecto colateral). La app debe:
  1. Recibir/reaccionar al push `device_revoked` (razón `password_changed`) en su conexión `ws/device/` abierta -- mismo manejo que cualquier revocación forzada (ver `07_dispositivos_vinculados.md`, punto "a").
  2. Volver a loguearse con la contraseña nueva -- **ambas llamadas**, no solo una: PanAccess (contenido/streaming) y `/api/auth/login/` (JWT de este documento) -- ver `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 0 y `05_login_social_tv.md`.
  3. Volver a abrir `ws/device/` y registrar el dispositivo de nuevo (el `device_token` viejo ya no sirve).
- No hace falta ningún manejo especial de reCAPTCHA acá -- este endpoint no lo usa (a diferencia de eliminar cuenta y olvidar contraseña).
