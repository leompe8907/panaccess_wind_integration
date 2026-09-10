# Login social (Google/Facebook) y su uso para autorizar una TV

Estado: backend listo. Uso básico de login social (sin pareo de TV) implementado en appVideo. El uso combinado con pareo de TV (agregar `udid`/`temp_token` al mismo login) es el que le toca implementar a iOS/Android si van a soportar "vincular TV escaneando QR" -- ver también `06_vincular_dispositivo.md`.
Referencia: `docs/GUIA_INTEGRACION_UNIFICADA.md` secciones 1.2, 2.1, 2.2, 2.2.1.

## Contrato de login social (uso normal, sin TV)

`POST /wind/auth/google/` o `POST /wind/auth/facebook/`, body `{"access_token": "<id_token de Google o access token de Facebook>"}` → `{"access","refresh","user":{"pk","email","first_name","last_name","subscriber_code"},"panaccess_credentials"}`. `panaccess_credentials` es `null` o `{"login1","password","login2","subscriberCode"}` -- ver la nota de seguridad más abajo antes de loguear o mostrar ese campo.

`SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER` (flag de negocio, hoy `false`): si se activa, un login social con correo sin suscriptor existente responde 400 con `non_field_errors: ["SubscriberNotFound: ..."]` -- detectar por la subcadena `"SubscriberNotFound"`, no por el texto completo.

## Contrato combinado: login social + autorizar una TV

Este es el diseño "solo autorizar la TV": el password real de PanAccess nunca llega al celular, solo viaja cifrado del backend a la TV por su propio WebSocket. Se activa agregando `udid` y `temp_token` (leídos del QR de la TV, ver `06_vincular_dispositivo.md`) al mismo body de `POST /wind/auth/google|facebook/`. Sin esos dos campos, el login social funciona exactamente igual que el uso normal de arriba -- es un cambio 100% aditivo.

Respuesta con `udid`/`temp_token` en el body: `panaccess_credentials` siempre `null` (nunca se filtra), más `udid_pairing`: `{"ok":true,"udid","subscriber_code"}` o `{"ok":false,"code":"missing_params"|"invalid_udid"|"invalid_temp_token"|"expired"|"not_pending"|"rate_limited"|"subscriber_not_found"|"subscriber_unresolved"|"internal_error","error":"..."}`.

Del lado de la TV no cambia nada respecto al pareo manual -- sigue esperando en `ws/auth/` y recibe el mismo `auth_with_udid:result` (ver `06_vincular_dispositivo.md`).

## Comportamiento de UX esperado (esto es lo que suele hacerse mal)

El punto de partida real es **el usuario ya logueado en la app, no una pantalla de login**. El backend describe el paso como "hacer login social", pero eso es la implementación del lado del servidor -- no lo que la app le debe mostrar a la persona:

1. Tiene que existir un punto de entrada dentro de la app ya logueada (ej. botón "Vincular TV" en inicio/ajustes) que abra la cámara para escanear el QR -- nunca la pantalla de login.
2. La app decodifica el QR y extrae `udid` + `temp_token` (formato del QR en `06_vincular_dispositivo.md`).
3. **La app obtiene un token de Google/Facebook de forma silenciosa**, usando la sesión ya activa del SDK (Google Sign-In / Facebook SDK) -- sin mostrarle al usuario ningún prompt de login. Si la sesión del SDK expiró, ahí sí corresponde pedir que inicie sesión, pero es el caso borde, no el camino normal.
4. Con ese token, la app llama a `POST /wind/auth/google|facebook/` agregando `udid`+`temp_token`. La respuesta trae un JWT nuevo -- si el usuario ya tenía uno, simplemente se reemplaza (es la misma cuenta).
5. La app no tiene que hacer nada más salvo mostrar el resultado: éxito (`udid_pairing.ok: true`) → "TV vinculada"; error → mensaje según `udid_pairing.code`, casi siempre "pedí un código nuevo en la TV" porque son de un solo uso y expiran a los 5 minutos.

Para el usuario, todo el proceso debe verse como "tocar un botón, escanear, ver confirmación" -- nunca como un segundo login visible, aunque técnicamente el backend lo procese como tal.

## Login manual: recordar que son dos llamadas, no una

No es login social, pero aplica al mismo flujo de autenticación en general (sección 0 de la guía): el login manual (`POST /api/auth/login/`) es una llamada aparte de PanAccess (`clientLogin`, contenido/streaming). Tras cambiar la contraseña (ver `02_cambiar_contrasena.md` y `03_olvidar_contrasena.md`) hay que repetir **ambas**, no solo una.

## Estado en appVideo (web)

Login social básico implementado. El combinado con `udid`/`temp_token` (pareo de TV desde el celular vía login social) es responsabilidad de mobile -- appVideo es la web/TV, no el celular que escanea.

## Qué debe implementar iOS/Android nativo

- El flujo de UX completo descrito arriba (punto de entrada dentro de la app logueada, obtención silenciosa del token del SDK, agregar `udid`/`temp_token` al body del login social).
- Manejar los 8 códigos de `udid_pairing.code` con mensajes claros, no un error genérico.
- Si el negocio activa `SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER`, detectar `"SubscriberNotFound"` en `non_field_errors`.

## Nota de seguridad: contraseña en texto plano

Dos lugares donde la contraseña real del suscriptor viaja en texto plano, por diseño explícito del cliente de negocio (riesgo ya aceptado, no es un pendiente de implementación):

1. **Correo de bienvenida** (tras alta de suscriptor): muestra usuario y contraseña de PanAccess en texto plano para que el suscriptor pueda loguearse la primera vez.
2. **Esta misma respuesta de login social**: `panaccess_credentials` puede incluir `password` en texto plano.

Qué debe tener en cuenta el equipo de apps (sin acción obligatoria): no loguear `panaccess_credentials.password` ni el contenido del correo de bienvenida en ningún sistema de logs/crash reporting (Sentry, consola de producción, etc.); si se muestra este dato en alguna pantalla, considerar enmascararlo por defecto con una opción de "mostrar" explícita.
