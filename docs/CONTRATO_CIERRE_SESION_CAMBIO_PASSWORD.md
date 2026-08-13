# Contrato: cierre de sesiones activas al cambiar/recuperar contraseña

Documento para los equipos de iOS y Android. Cubre exactamente una cosa: qué
le llega a una app cuando el usuario cambia su contraseña (desde el perfil) o
la recupera ("olvidé mi contraseña"), y qué debe hacer la app con eso para
cerrar sesión en los demás dispositivos.

Es un extracto enfocado de `docs/GUIA_INTEGRACION_UNIFICADA.md` (secciones 2,
4 y 5), que sigue siendo la referencia completa si hace falta algo de login,
pareo de TV o el resto de "dispositivos vinculados" que no esté acá. El
protocolo es el mismo para TV, mobile y web -- **appVideo ya lo tiene
implementado en producción** (`src/services/deviceSessionService.js` +
`src/hooks/useAppLifecycle.js`) y puede usarse como referencia.

## 1. Qué dispara esto

Tres acciones en el backend, todas con el mismo efecto:

- Cambiar contraseña desde el perfil (`POST /api/v1/profile/password/`).
- Confirmar "olvidé mi contraseña" (`POST /api/auth/password/reset-confirm/`).
- Cierre de cuenta.

## 2. Qué pasa automáticamente en el backend (la app no hace nada para esto)

En cuanto cualquiera de esas acciones tiene éxito, el backend por su cuenta:

1. Invalida todos los JWT ya emitidos para ese usuario (access y refresh). Cualquier request autenticado con un token viejo empieza a fallar.
2. Revoca **todos** los `DeviceSession` de la cuenta -- incluida la sesión que hizo el cambio.

El punto 1 es pasivo: la app solo se entera cuando su próxima llamada REST
falla. El punto 2 es el que da aviso **en vivo** si la app tiene el
WebSocket de la sección 3 abierto en ese momento -- es el mecanismo real
para forzar el cierre de sesión sin esperar a que la app haga una llamada.

## 3. El mecanismo en vivo: WebSocket `ws/device/`

### 3.1 Conectar

```
wss://backend.wind.do/ws/device/?token=<access JWT>
```

El JWT va como query param (no todas las plataformas pueden mandar headers
en el handshake de un WebSocket). Se abre **después de cualquier login**
exitoso -- manual, social, o justo después de un cambio/recuperación de
contraseña (que ya obliga a loguearse de nuevo con el JWT nuevo de todos
modos).

Cierres posibles al conectar:

| Código | Motivo |
|---|---|
| `4001` | JWT inválido/expirado, o límite de conexiones excedido (viene con motivo y segundos de espera). |
| `4004` | JWT válido pero no se pudo resolver el `subscriber_code` de la cuenta. |

### 3.2 Registrar el dispositivo

Primer mensaje que manda la app:

```json
{"type": "register_device", "device_type": "iOS", "device_model": "iPhone 14", "device_token": "<opcional, el guardado la vez anterior>"}
```

`device_type` recomendado: `iOS` o `android` (cualquier otro string se
acepta igual, pero conviene usar estos para que el dashboard filtre bien).
`device_token` se manda vacío/omitido la primera vez; en conexiones
siguientes se reenvía el que el servidor haya devuelto antes, para
refrescar el mismo registro en vez de crear uno duplicado.

Respuesta del servidor:

```json
{"type": "device_registered", "id": 123, "device_token": "<token>", "is_new": true}
```

Guardar `device_token` **e** `id` de forma segura y persistente (Keychain en
iOS, Keystore/EncryptedSharedPreferences en Android).

### 3.3 El aviso que importa: `device_revoked`

Cuando el backend revoca el/los dispositivo(s) -- incluido el caso de
cambio/recuperación de contraseña -- cada dispositivo afectado que siga
conectado recibe:

```json
{"type": "device_revoked", "reason": "password_changed"}
```

(`reason` también puede ser `"revoked_by_subscriber"` si el usuario lo
desvinculó a mano desde el panel, o `"account_closed"`; para este documento
el valor relevante es `"password_changed"`, pero la app debe reaccionar
igual sin importar el motivo). El backend cierra la conexión inmediatamente
después de mandar este mensaje.

**Qué debe hacer la app al recibirlo -- es un logout forzado completo, no
solo limpiar el `device_token`:**

1. Borrar `device_token`/`id` guardados.
2. Cerrar la sesión completa (JWT local, y cualquier credencial/sesión
   separada del sistema de contenido/streaming si la app la maneja aparte).
3. Navegar a la pantalla de login.

Esta última parte es la que más fácil se pasa por alto: es común implementar
el WebSocket para que, al recibir `device_revoked`, solo cierre el socket y
borre el token -- y la app se queda viéndose logueada, con una sesión que el
backend ya considera terminada, hasta que el usuario toca algo y falla. Si
la capa de red está separada de la capa de UI (patrón común en iOS/Android),
conviene un mecanismo de notificación global (delegate, notification center,
event bus, callback registrado a nivel de app) para que la capa de red le
pida a la UI que navegue, en vez de solo loguear el evento.

### 3.4 Si la app no estaba conectada en ese momento

No pasa nada en el instante -- no hay nadie escuchando. El efecto se aplica
igual la próxima vez que esa app intente reconectarse o refrescar su
registro con el `device_token` guardado: el backend lo rechaza con

```json
{"type": "error", "code": "device_token_invalid", "detail": "device_token no reconocido"}
```

y cierra la conexión. La app debe tratar esto exactamente igual que un
`device_revoked` en vivo (logout completo), no como un error transitorio a
reintentar.

### 3.5 Mantener la conexión viva

El servidor manda `{"type": "ping"}` cada 30 segundos; la app debe responder
`{"type": "pong"}`. Si el sistema operativo cierra el socket al pasar la app
a background, hay que reabrirlo al volver a foreground (mismo criterio que
`ensureDeviceSessionConnected` en `useAppLifecycle.js` de appVideo) -- de lo
contrario la app queda sorda a `device_revoked` hasta el próximo login
manual, aunque el `device_token` siga siendo válido.

## 4. Checklist mínimo para iOS/Android

- Después de todo login (manual, social, o tras cambiar/recuperar
  contraseña), abrir `ws/device/` con el JWT nuevo y mandar `register_device`.
- Guardar `device_token`/`id` en Keychain/Keystore, reenviar `device_token`
  en la siguiente conexión.
- Al recibir `device_revoked` (cualquier `reason`) o `device_token_invalid`:
  logout completo + volver a la pantalla de login. No alcanza con borrar el
  token local.
- Reabrir el WebSocket cuando la app vuelve a foreground si no sigue
  conectado.
- Responder `pong` a cada `ping` del servidor.
- Probar explícitamente: cambiar la contraseña desde el dispositivo A
  mientras el dispositivo B tiene sesión abierta -- B debe cerrar sesión
  solo, sin que el usuario tenga que tocar nada.

## 5. Referencia

Detalle completo del protocolo (incluye login, pareo de TV, listar/revocar
dispositivos a mano, y lecciones de una integración real con más casos
límite): `docs/GUIA_INTEGRACION_UNIFICADA.md`, secciones 2, 4 y 5.
Implementación de referencia funcionando: `D:\appVideo\src\services\deviceSessionService.js`
y `D:\appVideo\src\hooks\useAppLifecycle.js`.
