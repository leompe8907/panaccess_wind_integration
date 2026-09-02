# Activación de login por UDID (pareo TV/QR) para el brand `wind`

Fecha: 2026-09-02
Repos: `appVideo` (config), `Back-Wind-V2` (backend, sin cambios).

## Contexto

El flujo de login remoto por UDID (la TV pide un código, el usuario lo confirma desde
móvil/web, el backend manda las credenciales cifradas por WebSocket) ya estaba
implementado de punta a punta en el backend de Wind desde antes de esta sesión:
rutas (`wind/urls.py`), modelo (`UDIDAuthRequest`), servicio
(`wind/services/udid_auth_service.py`) y cifrado (`wind/utils/crypto_tv.py`,
esquema moderno de llave efímera + AES-GCM, siempre autenticado).

Para el brand `wind` en `appVideo`, el bloque `login.udid` estaba con
`enabled: false` y `baseUrl`/`requestPath`/`wsUrl` vacíos -- **apagado
intencionalmente**, no roto. `tempTokenRequired: true` ya estaba seteado desde el
28/08 (mitigación preventiva Medio #8) para que, el día que se activara, arrancara
directo con el esquema moderno.

## Qué se implementó

Un solo cambio, en `appVideo/src/config/brands.js`, bloque `login.udid` del brand
`wind`:

| Campo | Antes | Ahora |
|---|---|---|
| `enabled` | `false` | `true` |
| `baseUrl` | `""` | `"https://backend.wind.do"` |
| `requestPath` | `""` | `"/wind/request-udid-manual/"` |
| `wsUrl` | `""` | `"wss://backend.wind.do/ws/auth/"` |

Resto de los campos (`appType`, `appVersion`, `maxReconnectAttempts`, `reconnectMs`,
`heartbeatMs`, `privateKeyUrl: ""`, `tempTokenRequired: true`) sin cambios.

**No se tocó Back-Wind-V2.** El backend ya soportaba el flujo completo; no hizo
falta ninguna migración, endpoint ni cambio de config del lado del servidor.

## Por qué `requestPath` y `wsUrl` explícitos (no confiar en el default)

`useUdidLoginFlow.js` (hook compartido por todos los brands) tiene un fallback:

```js
buildApiUrl(config?.baseUrl, config?.requestPath || '/udid/request-udid-manual/')
```

Ese default (`/udid/...`) no existe en el backend de Wind -- las rutas reales
están bajo el prefijo `/wind/` (`panaccess_wind_integration/urls.py`:
`path('wind/', include('wind.urls'))`). Dejar `requestPath` vacío para `wind`
habría producido un 404 real en producción. Por eso se setea explícito acá y
**no se tocó el default del hook** -- otros brands pueden depender de ese
default apuntando a un backend distinto (el proyecto `udid`/`FrontUdid` de
cableatlantico, que sí usa el prefijo `/udid/`), y no es parte de este cambio
tocar eso.

El WebSocket (`wsUrl`) va **sin** prefijo `/wind/`: confirmado en
`panaccess_wind_integration/asgi.py`, el `URLRouter` de `wind.routing` cuelga
directo de la raíz (`ws/auth/`), no bajo `wind/`.

## Verificación hecha

- `node --check src/config/brands.js` -- sintaxis OK.
- Revisión de `wind/urls.py` y `panaccess_wind_integration/asgi.py` -- rutas HTTP
  y WS confirmadas contra el código real, no asumidas.
- Cambio aislado al bloque `wind`; los demás brands (bromteck, cableatlantico,
  intv, etc.) no se tocaron.

## Qué falta para que esto sea útil en producción (no técnico)

El toggle es seguro -- si nadie del otro lado sabe usar la pantalla de pareo,
simplemente no se usa, no rompe nada. Pero para que aporte valor real falta
confirmar si existe una pantalla (app nativa Android/iOS, o el mismo
`appVideo` en modo web/móvil) que muestre el flujo de "ingresá este código" del
lado del celular/web -- eso no se verificó en este cambio, quedó pendiente de
coordinación con el equipo de apps.

## Rollback

Si hace falta revertir: volver `enabled` a `false` en el bloque `udid` del brand
`wind` (o vaciar `baseUrl`) y rebuildear `appVideo`. No requiere tocar el
backend.
