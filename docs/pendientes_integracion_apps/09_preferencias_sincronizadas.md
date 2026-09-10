# Preferencias sincronizadas (control parental + favoritos)

Estado: backend listo (2026-08-31). Implementado en appVideo. iOS/Android puede consumirlo directo, sin implementar "dispositivos vinculados" primero -- son features independientes.
Referencia: `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 6, `docs/SINCRONIZACION_PREFERENCIAS_2026-08-31.md`.

## Qué es

Sincroniza entre todos los dispositivos de una cuenta el control parental **propio de la app** (PIN local, canales bloqueados, clasificación por edad) y la lista de canales favoritos -- antes vivían solo en almacenamiento local de cada dispositivo, sin compartirse entre ellos.

**No tiene nada que ver con el PIN de perfil de PanAccess** (ese viene de la smartcard, se lee automático y el usuario nunca lo escribe) -- son dos sistemas de PIN totalmente separados. No confundir uno con el otro al implementar la UI.

## Prerrequisito: solo el JWT, nada más

Alcanza con el JWT de sesión (`POST /api/auth/login/` o login social) -- **no depende de "dispositivos vinculados"** (`07_dispositivos_vinculados.md`): no hace falta abrir `ws/device/` ni tener un `device_token` registrado para usar este endpoint, son features independientes que solo comparten el mismo JWT. En appVideo-web ese JWT solo se obtiene cuando `login.deviceSession.enabled` está activo por brand -- eso es una decisión de implementación de ese cliente puntual, no una restricción del backend. **iOS/Android puede llamar a este endpoint apenas tenga el JWT, sin necesidad de implementar dispositivos vinculados primero.**

## `profileKey`: cómo se enlaza cada dispositivo al perfil correcto

Cada cuenta puede tener varios perfiles tipo Netflix, administrados enteramente por PanAccess (no por este backend). La preferencia se guarda por el par `(subscriber_code, profileKey)`:

- Si la cuenta tiene un perfil PanAccess activo/elegido, `profileKey` = el mismo identificador de perfil que el cliente ya usa para las llamadas a PanAccess.
- Si la cuenta no tiene perfiles activados, o todavía no se eligió ninguno, `profileKey` = `"default"` (o se omite -- el backend usa `"default"` por omisión).

El `subscriber_code` nunca lo manda el cliente -- se resuelve del JWT. **Migración automática (una sola vez):** la primera vez que una cuenta usa un `profileKey` real (no `"default"`), el backend copia automáticamente lo que había bajo `"default"` hacia ese perfil nuevo -- transparente para el cliente, solo hay que mandar el `profileKey` correcto en cada llamada.

## Leer preferencias guardadas

`GET /api/v1/preferences/?profileKey=<opcional>` (JWT). Sin `profileKey`, usa `"default"`.

```json
{
  "success": true,
  "profileKey": "default",
  "parental": {
    "enabled": true,
    "pinHash": "...", "pinSalt": "...", "pinMethod": "pbkdf2", "pinIterations": 100000,
    "blockedChannelIds": ["45", "112"],
    "ratingEnabled": true, "ratingAllowedMax": 13, "ratingApplyToLive": true
  },
  "favorites": ["101", "202", "310"]
}
```

`parental` viene `null` si la cuenta nunca guardó control parental; `favorites` viene `[]` si nunca guardó favoritos -- tratar ambos como "sin configurar todavía", no como error.

**Importante -- qué NO trae `parental`:** solo la configuración durable (PIN, canales bloqueados, clasificación). Nunca incluye estado de desbloqueo temporal (ej. "desbloqueado por 30 minutos") -- eso es intencionalmente local a cada dispositivo, para que un desbloqueo en un dispositivo no desbloquee sin querer el control parental en otro. Si la app maneja un PIN propio, el estado de "sesión desbloqueada" va solo en local, nunca a este endpoint.

## Guardar un cambio

`PUT /api/v1/preferences/` (JWT), body con **solo los campos que cambiaron** -- actualización parcial: mandar `favorites` no borra `parental` ya guardado, y viceversa.

```json
{ "profileKey": "default", "favorites": ["101", "202", "310", "415"] }
```

Respuesta 200: el estado completo actualizado (incluye también lo que no cambió).

**Validación (400, `{"success": false, "errors": {...}}`):** `parental` debe ser objeto JSON, máx ~20KB; `favorites` lista de strings, máx 500 elementos; `profileKey` opcional, en blanco/ausente cae a `"default"`. Otros errores: 404 si el JWT no resuelve `subscriber_code`. Rate limit: mismo `ProfileThrottle` que el resto de `/api/v1/profile/...`.

## Patrón recomendado del lado cliente (no es parte del contrato del backend, pero conviene replicarlo)

- **Push (`PUT`) inmediato** cada vez que el usuario cambia algo -- fire-and-forget: si falla (sin red, JWT vencido) no debe bloquear ni revertir el cambio local, que ya se guardó antes de llamar al backend; alcanza con reintentar en el próximo evento de sync.
- **Pull (`GET`) en dos momentos:** al iniciar/reanudar la app, y al volver de background a foreground. **No hay push en tiempo real** hacia otros dispositivos conectados en simultáneo -- a diferencia de "dispositivos vinculados" (sección 4), este endpoint no tiene canal WebSocket. Si dos dispositivos de la misma cuenta están abiertos en primer plano a la vez, un cambio en uno no le llega al otro hasta que ese otro pase por background/reapertura. Esto es una limitación conocida, no un bug -- si el producto lo necesita a futuro, se podría avisar por el mismo canal WebSocket de dispositivos vinculados (`subscriber_devices_{subscriber_code}`), sin un canal nuevo.

## Estado en appVideo (web)

Implementado siguiendo exactamente el patrón de arriba.

## Qué debe implementar iOS/Android nativo

- `GET`/`PUT /api/v1/preferences/` con JWT -- puede implementarse independientemente de "dispositivos vinculados" (07), no hace falta esperar a tener eso resuelto.
- Resolver correctamente `profileKey` si la app soporta perfiles tipo Netflix -- usar el mismo identificador que ya usa para las llamadas a PanAccess, no inventar uno propio.
- Separar claramente en la UI el PIN de control parental de este sistema del PIN de perfil de PanAccess -- son cosas distintas para el usuario aunque técnicamente no se relacionen en el backend.
- Guardar el estado de "desbloqueo temporal" (si la app lo tiene) solo en local, nunca mandarlo a `PUT /api/v1/preferences/`.
- Implementar el patrón push-inmediato/pull-en-dos-momentos, y tener en cuenta que no hay sincronización en vivo entre dos dispositivos abiertos a la vez -- no es un bug a reportar si se nota esa demora.
