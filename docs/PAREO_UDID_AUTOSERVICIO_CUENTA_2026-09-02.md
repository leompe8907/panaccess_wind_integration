# Pareo UDID auto-servicio desde el dashboard web (Medio #8)

Fecha: 2026-09-02
Referencia: `docs/ACTIVACION_UDID_WIND_2026-09-02.md`, `docs/INTEGRACION_PAREO_TV_DISPOSITIVOS.md`, `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (Medio #8).

## Contexto

El pareo de TV que ya existía (`docs/INTEGRACION_PAREO_TV_DISPOSITIVOS.md`) depende de que
la app móvil nativa (iOS/Android) escanee un QR y confirme con login social -- ese trabajo
del lado de las apps seguía pendiente. Se pidió una alternativa estilo Prime Video: el
usuario ve un código corto en pantalla (TV, celular o web -- `login.udid` es el mismo
componente en `appVideo` para cualquiera de los tres, no algo exclusivo de Smart TV) y lo
escribe en una pantalla del dashboard web de Wind, sin depender de ninguna app móvil.

El endpoint existente para asociación manual (`POST /wind/validate-and-associate-udid/`,
`ValidateAndAssociateUDIDView`) exige `temp_token` -- el secreto real del pareo, que nunca
se muestra en texto (solo viaja adentro del QR, ver `appVideo/src/pages/LoginPage.jsx`).
No servía tal cual para un campo de texto tipeable a mano.

## Decisión de seguridad

Se evaluaron tres opciones (mostrar el `temp_token` también en texto, escanear QR desde la
web con cámara, o aceptar solo el `udid` corto para una cuenta autenticada). Se eligió la
tercera, con una condición: agregar un rate limit **por cuenta** que hoy no existía.

Se verificó en el código que `check_udid_rate_limit` limita intentos contra **un** `udid`
específico (20/hora por `udid`) -- eso no protege nada contra alguien logueado que prueba
muchos `udid` distintos, cada uno con su propio cupo. Sin un límite por cuenta, quitar la
exigencia de `temp_token` habría sido una regresión real de seguridad (fuerza bruta sobre
el espacio de 32 bits del `udid`, `secrets.token_hex(4)`).

## Qué se implementó

### 1. Rate limit por cuenta (`wind/utils/websocket_utils.py`)

```python
def check_udid_account_rate_limit(subscriber_code, max_requests=5, window_minutes=15):
    cache_key = f"rate_limit:udid_account:{subscriber_code}"
    return _reserve_atomic_slot(cache_key, max_requests, window_minutes * 60)
```

5 intentos / 15 minutos por `subscriber_code`, sin importar cuántos `udid` distintos se
prueben. Con esto, enumerar el espacio de 32 bits del `udid` queda fuera de alcance práctico
incluso si el atacante controla varias cuentas (cada una limitada igual).

### 2. Nuevo endpoint (`wind/views.py`, `AssociateUDIDByAccountView`)

`POST /wind/associate-udid-by-account/` -- **`IsAuthenticated`** (JWT), body `{"udid": "..."}`
únicamente. **Deliberadamente no reutiliza** `ValidateAndAssociateUDIDView` ni su
`UDIDAssociationSerializer` (el flujo QR/operador con `temp_token`) -- quedan intactos, cero
cambio de comportamiento ahí. Es una vía nueva y separada:

- `subscriber_code` y `sn` (smartcard) se resuelven **siempre del lado del servidor**, a
  partir del JWT autenticado (`resolve_subscriber_code_for_user` + `get_smartcards_for_subscriber`,
  cruzando contra `SubscriberInfo`) -- nunca se aceptan del body. No hay forma de que este
  endpoint asocie el `udid` a una cuenta que no sea la propia del usuario logueado.
- Verifica rate limit por cuenta (429 con `retry_after` si se excede), existencia/expiración/
  estado `pending` del `UDIDAuthRequest`, que la cuenta tenga una smartcard resuelta, que esa
  smartcard no esté ya vinculada a otro pareo activo, y que la cuenta no esté bloqueada
  (`SubscriberInfo.is_locked()`).
- Reutiliza exactamente el mismo mecanismo de entrega que el flujo QR: al validar, dispara
  `channel_layer.group_send("udid_{udid}", {"type": "udid.validated", ...})` -- el consumer
  de WebSocket (`AuthWaitWS.udid_validated`, sin cambios) es quien de verdad cifra y entrega
  las credenciales al dispositivo que está esperando. **No se tocó `consumers.py` ni
  `udid_auth_service.py`.**
- Auditoría: mismo `action_type="udid_used"` que el flujo QR, con `details.association_method
  = "account_self_service"` para poder distinguirlos en los logs sin migrar nada.

### 3. Sección "Vincular dispositivo" (`wind/templates/wind/dashboard.html`)

Nueva pestaña en el dashboard (junto a "Dispositivos"), visible solo si la cuenta tiene
suscriptor vinculado (mismo criterio que "Productos"/"Dispositivos"). Un solo campo de texto
+ botón; llama a `WindAuth.fetchApi("/wind/associate-udid-by-account/", {...})` y muestra el
resultado. Sin reCAPTCHA (mismo criterio que la sección de "Dispositivos vinculados": ya está
detrás de JWT + su propio rate limit dedicado).

## Verificación hecha

- `py_compile` + `pyflakes` sobre los archivos tocados -- limpio (el único warning de
  pyflakes es preexistente, en código no tocado).
- `manage.py check` -- sin problemas.
- Template `dashboard.html` renderizado de punta a punta con `get_template().render()`
  (incluye `collectstatic` para no saltarse el pipeline de estáticos) -- sin errores,
  confirmado que el HTML/JS nuevo aparece en la salida.
- 9 tests nuevos (`wind/tests/test_udid_account_association.py`), corridos contra Postgres
  real (`pgserver`, efímero) + cache real (LocMemCache, sustituyendo Redis no disponible en
  sandbox): sin autenticar, sin suscriptor vinculado, `udid` inexistente, expirado, no
  pendiente, cuenta sin smartcard, smartcard ya vinculada a otro pareo, rate limit por cuenta
  (429), y camino feliz completo -- incluida la notificación real por WebSocket
  (`transaction.on_commit` capturado explícitamente, no solo mockeado a medias). 9/9 OK.

## Qué queda igual (fuera de alcance de este cambio)

- El flujo QR/login social por app móvil (`docs/INTEGRACION_PAREO_TV_DISPOSITIVOS.md`) no se
  tocó -- sigue siendo el camino "principal" si algún día las apps nativas lo implementan.
- `ValidateAndAssociateUDIDView` (el endpoint con `temp_token`, usado por operador/soporte o
  por lo que las apps móviles terminen consumiendo) no cambió en nada.
- Si una cuenta tiene más de una smartcard, este endpoint usa la primera que tenga fila en
  `SubscriberInfo` (no hay selector de "cuál dispositivo/smartcard usar" en esta primera versión) --
  suficiente para el caso típico de una smartcard por cuenta; si hace falta elegir entre
  varias, es una mejora de UI a futuro, no un problema de seguridad.
