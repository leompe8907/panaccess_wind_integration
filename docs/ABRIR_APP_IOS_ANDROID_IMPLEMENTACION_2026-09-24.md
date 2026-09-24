# Abrir la app de WindTV desde el backend -- implementación

Fecha: 2026-09-24
Pedido original: `docs/ABRIR_APP_DESDE_BACKEND_WIND.md` (equipo de apps iOS/Android, mismo día).

## Qué se implementó

### 1. `go_windtv_view` (`wind/views.py`)

- **Android:** sin cambios -- sigue funcionando con el `intent://` + fallback a Play Store.
- **iPhone/iPad/iPod y cualquier otro caso (antes caía directo a la web):** ahora renderiza `wind/go_windtv.html` en vez de redirigir. Esto es necesario porque Safari no abre un Universal Link si la navegación viene de una redirección automática -- hace falta que el usuario toque un botón.
- El servidor no distingue iPad de Mac (mismo User-Agent desde iPadOS 13) -- esa distinción se resolvió del lado del cliente, no del servidor (ver siguiente punto).

### 2. `wind/templates/wind/go_windtv.html` (nueva)

Página con un botón "Abrir WindTV" (`windtv://open`). El JS decide en el navegador:
- Si es iPhone/iPod (por User-Agent) o iPad (por `Macintosh` + `maxTouchPoints > 1`, ya que una Mac real no tiene pantalla táctil): muestra el panel con el botón.
- Si no (computadora real): `location.replace()` a la web de inmediato, sin que el usuario vea nada.
- El botón, al tocarse, espera 1.5s y si la pestaña sigue visible (la app no abrió), cae a la App Store o a la web mientras no esté publicada.
- A propósito no se dispara `windtv://open` solo al cargar la página -- si la app no está instalada, Safari muestra un error en vez de simplemente no hacer nada.

### 3. `appConfig.py` (`EmailConfig`)

Claves nuevas: `WINDTV_IOS_SCHEME` (`windtv`), `WINDTV_IOS_BUNDLE_ID` (`com.windtelecom.windtv`), `WINDTV_IOS_TEAM_ID` (`TXH7LJ7B2S`), `WINDTV_IOS_APP_STORE_URL` (vacía hasta publicar), `WINDTV_ANDROID_SHA256` (vacía -- **pendiente del equipo de Android**, ver más abajo).

De paso, `GOOGLE_PLAY_URL` (usada en los correos de bienvenida/cambio de contraseña/eliminación) dejó de tener un valor fijo separado y ahora se deriva de `WINDTV_ANDROID_PACKAGE` cuando no hay override explícito por `.env` -- corrige el hallazgo Bajo #39 de `AUDITORIA_CONSOLIDADA_2026-08-24.md` (apuntaba al package viejo `com.wind.windtv`) y evita que los dos vuelvan a desincronizarse en el futuro. **Si el `.env` de producción tiene `WIND_APP_GOOGLE_PLAY_URL` seteada con el valor viejo, ese override sigue ganando** -- hay que corregirla ahí (o quitarla, para que tome el valor derivado).

### 4. Archivos de verificación de dominio (`deploy/well-known/`)

- `apple-app-site-association`: mapea `/wind/go/windtv/*` y `/wind/l/v1/*` (QR de vincular TV) al Team ID + Bundle ID de iOS.
- `assetlinks.json`: mapea el package de Android -- **el campo `sha256_cert_fingerprints` tiene un placeholder (`REEMPLAZAR_CON_SHA256_DE_FIRMA_DE_PLAY`) porque todavía no tenemos el SHA-256 real.** Sin el valor correcto, Android nunca verifica el App Link (pero el esquema `windtv://open` vía `intent://` sigue funcionando igual, no depende de este archivo).

Estos archivos viven en el repo (no en `/srv/wind/well-known/` como sugería el pedido original) para que se actualicen solos con cada `git pull`, en vez de mantenerlos aparte a mano.

### 5. `deploy/nginx/panaccess-wind.conf` y `panaccess-wind-scaled.conf`

Se agregaron los dos `location` que sirven esos archivos directo desde nginx (sin pasar por Django -- ni auth, ni CORS, ni redirecciones), apuntando a `/opt/panaccess-wind/deploy/well-known/`. **Esto es una plantilla del repo -- no se tocó nginx en el servidor real.** Falta aplicarlo (ver "Qué falta hacer en el servidor" abajo).

### 6. Tests (`wind/tests/test_go_windtv_view.py`)

4/4 OK (`manage.py test wind.tests.test_go_windtv_view`, sin necesidad de base de datos real -- `SimpleTestCase`). Cubren: Android sin cambios, iPhone renderiza la página intermedia con el contexto correcto, un UA de Mac de escritorio también cae a la página intermedia (la decisión final la hace el JS, no se puede probar desde un test de Django), y que el link de App Store no aparece mientras `WINDTV_IOS_APP_STORE_URL` esté vacía.

## Qué falta para que esto funcione en producción

1. **El SHA-256 del certificado de firma de Play** (Play Console → la app → Integridad de la app → Firma de apps) -- reemplazar el placeholder en `deploy/well-known/assetlinks.json`. Sin esto, el App Link de Android no verifica (el `intent://` de siempre sigue andando).
2. **Aplicar el cambio de nginx en el servidor** (no se hizo automáticamente):
   ```bash
   sudo cp /opt/panaccess-wind/deploy/nginx/panaccess-wind-scaled.conf /etc/nginx/sites-available/panaccess-wind.conf
   sudo nginx -t && sudo systemctl reload nginx
   ```
   (usar `panaccess-wind.conf` en vez de `-scaled.conf` solo si el servidor no corre las 8 instancias de Daphne).
3. **La cadena SSL completa de `backend.wind.do`** -- Apple y Google descargan estos archivos con validación TLS estricta; si la cadena sigue incompleta (`docs/COMANDOS_CADENA_SSL_BACKEND_WIND_DO_2026-09-22.md`, todavía sin confirmar que se aplicó), la verificación de dominio puede fallar en silencio.
4. **Corregir `WIND_APP_GOOGLE_PLAY_URL` en el `.env` de producción** si tiene el valor viejo con `com.wind.windtv` -- si no, el override sigue ganando sobre el fix de código.
5. Cuando la app de iOS se publique: setear `WINDTV_IOS_APP_STORE_URL` en el `.env`.

## Cómo probarlo una vez desplegado

```bash
curl -sI https://backend.wind.do/.well-known/apple-app-site-association   # 200, Content-Type: application/json, sin Location
curl -sI https://backend.wind.do/.well-known/assetlinks.json              # idem
```

Verificador de Google: `https://digitalassetlinks.googleapis.com/v1/statements:list?source.web.site=https://backend.wind.do&relation=delegate_permission/common.handle_all_urls`

Manual: abrir `/wind/go/windtv/` desde un iPhone/iPad real (no alcanza con las devtools de desktop -- `maxTouchPoints` no se simula igual) con la app instalada -- debería mostrar el botón y, al tocarlo, Safari pregunta "¿Abrir en WindTV?". Apple descarga `apple-app-site-association` cuando se instala/actualiza la app, así que para probar el Universal Link real hay que reinstalarla después de publicar el archivo en el servidor.
