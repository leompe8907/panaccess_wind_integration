# bromteck: backendBaseUrl a HTTPS — Bajo #29

Fecha: 2026-08-28

## Qué

Hallazgo Bajo #29: el brand `bromteck` (`appVideo/src/config/brands.js`, bloque `BRANDS[0]`, líneas 256-662) tenía `login.socialLogin.backendBaseUrl` apuntando a `http://backend.wind.do` -- el mismo dominio de producción que el brand `wind` ya usa correctamente sobre `https://` (línea 1882 del mismo archivo). Cambiado a `https://backend.wind.do`. Se actualizó también el comentario de `deviceSession.baseUrl` (línea 358) que citaba la URL vieja como ejemplo, para que no quede desactualizado.

Antes de tocar nada se confirmó con el cliente que `bromteck` está dentro del alcance de esta auditoría (marca propia, no de un tercero) -- la fila original de la auditoría lo dejaba "pendiente del equipo de appVideo" precisamente por esa duda de alcance.

## Por qué

Login social (Google/Facebook) para este brand estaba habilitado (`google.enabled`/`facebook.enabled: true`) contra un endpoint en texto plano: las credenciales OAuth y cualquier token que viaje en ese POST inicial hacia `backendBaseUrl` (y las respuestas del backend) circulaban sin cifrado de transporte. `http://` también expone a downgrade/MITM en redes no confiables (WiFi público, por ejemplo, algo relevante para una app de TV/streaming). El propio backend Django (`backend.wind.do`) ya sirve por HTTPS -- el cliente HTTP nunca iba a poder completar la conexión en texto plano contra el puerto real de producción salvo que hubiera algún proxy intermedio degradando el esquema, así que el efecto práctico más probable de este bug era simplemente requests fallidos o un salto a HTTPS forzado en el navegador, pero como configuración explícita seguía siendo la incorrecta y el bug real a corregir en el archivo de configuración.

## Cómo se verificó

Se cargó el módulo `brands.js` con el import nativo de Node (ESM, ya que es un objeto plano sin JSX) y se confirmó que `getBrandConfig('bromteck').login.socialLogin.backendBaseUrl === 'https://backend.wind.do'`.

## Pendiente relacionado, no resuelto en este cambio

`bromteck.login.udid.baseUrl` y `wsUrl` siguen apuntando a `http://127.0.0.1:8001` / `ws://127.0.0.1:8001` con `udid.enabled: true` -- a diferencia del caso de `socialLogin`, acá no hay un dominio de producción HTTPS conocido al cual migrar: es una URL de desarrollo local. Si el pareo de TV por QR (login remoto UDID) está realmente activo en producción para este brand, hace falta que el equipo confirme contra qué backend real debería apuntar antes de tocarlo -- no es un simple cambio de protocolo como el de `socialLogin`, sino una decisión de qué servidor sirve ese flujo en producción para esta marca.

## Archivos tocados

- `appVideo/src/config/brands.js`
- `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (fila Bajo #29 actualizada)
