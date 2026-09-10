# Vincular dispositivo (pareo de TV por QR/código manual)

Estado: backend y TV (appVideo) listos. Falta trabajo específico de iOS/Android nativo para que el pareo sea fluido (hoy funciona vía navegador como stopgap).
Referencia: `docs/GUIA_INTEGRACION_UNIFICADA.md` secciones 1.1, 1.1.1, 2.2, 2.2.1, 2.5; `docs/PROPUESTA_FORMATO_QR_UDID_2026-09-02.md`; `docs/IMPLEMENTACION_FORMATO_QR_URL_2026-09-03.md`.

No confundir con `07_dispositivos_vinculados.md`: esto es el proceso de **autorizar una TV nueva** (algo que pasa una vez por TV); dispositivos vinculados es la lista de sesiones de la cuenta (TV, mobile, web) que ya se autenticaron, para poder verlas/revocarlas.

## El flujo completo, con el rol de cada plataforma

1. **La TV** pide un código: `GET /wind/request-udid-manual/` → `udid` (8 hex) + `temp_token` (secreto real, 43 caracteres). Rate limit documentado en `04_fingerprint.md`. La TV muestra el `udid` como texto y un QR aparte.
2. **La TV** se conecta a `ws/auth/` y espera el resultado (`auth_with_udid:result`) -- expira a los 5 minutos si nadie completa el pareo.
3. **El celular** escanea el QR (o el usuario escribe el `udid` a mano) y completa el pareo por uno de dos caminos:
   - **Login social** (principal, ver `05_login_social_tv.md`): agrega `udid`+`temp_token` al body de `POST /wind/auth/google|facebook/`.
   - **Pareo manual** (fallback, sin login social): `POST /wind/validate-and-associate-udid/`, body `{"udid","temp_token","subscriber_code","sn","operator_id","method":"automatic"|"manual"}` (`sn` es una smartcard del suscriptor, se puede traer de `GET /api/v1/profile/products/`). Respuesta 200 `{"message","udid","subscriber_code","smartcard_sn","status","validated_at"}`. Errores 400 (udid/temp_token/SN inválido, cuenta bloqueada, SN ya asociado a otro UDID) y 429 (1/min por udid).
4. El celular **no abre ningún WebSocket** para esto -- la TV recibe el resultado por su propio `ws/auth/` del paso 2.

## Formato del código y QR que muestra la TV (contrato)

- **Texto en pantalla:** el `udid` tal cual (8 hex, ej. `a1b2c3d4`).
- **Payload del QR** (generado client-side por appVideo, nunca lo arma el backend): `"{appName}:{udid}:{temp_token}"` -- ej. `"WindTV:a1b2c3d4:xJ9k2m..."`. Sin `temp_token` (retrocompatibilidad vieja), cae a `"{appName}:{udid}"`.
- **Reglas de parseo:** separar por `:` tomando los **últimos dos** segmentos como `udid` y `temp_token` (`rsplit(':', 2)`), NO asumir exactamente 3 partes -- `appName` es config de marca y puede contener `:`. `udid` y `temp_token` nunca contienen `:`.
- **Sin campo de versión** -- si este formato cambia, no hay forma de detectarlo desde el payload mismo. Confirmar con el equipo de TV antes de programar un parser en mobile, es un detalle de implementación de appVideo, no un estándar acordado.
- **El QR en sí también es una URL real** desde 2026-09-03: `https://backend.wind.do/wind/l/v1/{udid}/?t={temp_token}`. Cualquier cámara que lo escanee hoy abre esa URL en el navegador normal, que resuelve el pareo vía la web de auto-servicio (login si hace falta, precarga el código). Esto ya funciona sin que mobile haga nada -- ver siguiente sección para lo que falta si se quiere evitar el salto al navegador.

## Qué falta: Universal Link (iOS) / App Link (Android)

Hoy, escanear el QR con la cámara del sistema abre el navegador (funciona, pero no abre la app directo). Para que el sistema operativo abra la app nativa al escanear, hace falta configurar el dominio `backend.wind.do` como:

- **Android App Link:** `assetlinks.json` servido en `https://backend.wind.do/.well-known/assetlinks.json`, más los `intent-filter` correspondientes en el `AndroidManifest.xml`.
- **iOS Universal Link:** `apple-app-site-association` servido en `https://backend.wind.do/.well-known/apple-app-site-association`, más el entitlement `Associated Domains`.

Ninguno de los dos archivos existe todavía en el backend -- si mobile decide seguir este camino, coordinar con backend para publicarlos (son archivos estáticos, no requieren lógica de servidor). Si se implementa, la app debe extraer `udid` y `t` (`temp_token`) de la **URL**, no del formato viejo con `:` -- ese ya no lo genera la TV.

**Si no se implementa, nada se rompe** -- el QR sigue funcionando siempre a través del navegador y el flujo de auto-servicio web (sección siguiente). Es una mejora de UX, no un requisito para que el pareo funcione.

## Alternativa sin escanear QR (ya funciona hoy, no requiere trabajo de mobile)

El usuario puede leer el código corto (`udid`, sin `temp_token`) directamente de la pantalla que lo muestra y escribirlo en el dashboard web de Wind (`POST /wind/associate-udid-by-account/`, requiere sesión JWT en el navegador). Las apps pueden, opcionalmente, mostrar un mensaje tipo "¿no podés escanear? entrá a tu cuenta en la web" apuntando a esto, pero no es un requisito de esta guía.

## Comportamiento de UX esperado del lado de mobile

Ver `05_login_social_tv.md`, sección "Comportamiento de UX esperado" -- aplica igual acá porque el camino principal de pareo (login social) es el mismo flujo.

## Qué debe implementar iOS/Android nativo

- Escanear el QR y parsear su contenido con las reglas de arriba (o, si la cámara del sistema resuelve la URL directo por Universal/App Link, leer `udid`/`t` de la URL).
- Completar el pareo por login social (`05_login_social_tv.md`) o, si el usuario no tiene login social configurado, por el camino manual (`validate-and-associate-udid`, necesita el `sn` de `GET /api/v1/profile/products/`).
- Opcionalmente, coordinar con backend la publicación de `assetlinks.json`/`apple-app-site-association` si se quiere abrir la app directo al escanear en vez de pasar por el navegador.
- No hace falta implementar nada del formato viejo (`appName:udid:temp_token` sin URL) -- ya no lo genera ninguna TV desde 2026-09-03.

## Nota aparte: brand `bromteck` en appVideo todavía apunta a desarrollo local

No es un pendiente de iOS/Android, pero queda registrado acá porque es el mismo subsistema (pareo por UDID): en `appVideo/src/config/brands.js`, el brand `bromteck` tiene `login.udid.baseUrl`/`wsUrl` apuntando a `http://127.0.0.1:8001` (URL de desarrollo, nunca actualizada) -- a diferencia del `backendBaseUrl` de login social de ese mismo brand, que sí se corrigió a `https://backend.wind.do` el 2026-08-28. No hay un backend de producción conocido para el UDID de este brand todavía; falta que el cliente confirme si el pareo por QR está realmente activo en producción para `bromteck` y contra qué backend debería apuntar antes de poder corregirlo.
