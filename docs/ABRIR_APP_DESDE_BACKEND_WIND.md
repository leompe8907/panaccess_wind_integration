# Abrir la app de WindTV desde el backend

Pedido del equipo de apps (Android e iOS) al equipo de backend. Fecha: 24/09/2026.

## Qué pasa hoy y qué queremos

`/wind/go/windtv/` es la página "Ir a WindTV": la usan el botón de los correos (por ejemplo, "Contraseña actualizada") y el final del cambio de contraseña cuando se pide con `origin=app`. Hoy (`go_windtv_view`, `wind/views.py:1129-1179`):

- Android: redirige a un `intent://` que abre la app o, si no está, va a Google Play. **Funciona.**
- iPhone, iPad, iPod y el resto: redirige siempre a `https://windtv.wind.do/`. **La app de iOS nunca se abre.**

Lo probamos en un iPad: el cambio de contraseña funciona, pero al terminar queda en la web de WindTV.

**Comportamiento que pedimos:**

| Dispositivo | Con la app instalada | Sin la app |
|---|---|---|
| iPhone, iPad, iPod | Abre la app de WindTV | App Store (cuando esté publicada) o, mientras tanto, la web de WindTV |
| Android: teléfono y tablet | Abre la app de WindTV | Google Play |
| Web (computadora, cualquier navegador) | — | La web de WindTV (`https://windtv.wind.do/`), como hoy |

Datos de las apps:

| | iOS | Android |
|---|---|---|
| Identificador | Bundle ID `com.windtelecom.windtv` | Paquete `com.wind.android.streaming` |
| Equipo / firma | Team ID `TXH7LJ7B2S` | SHA-256 del certificado de firma de Play (lo pasa el equipo de Android) |
| Esquema propio | `windtv://open` | `windtv://open` (ya existe) |
| Tienda | Todavía no publicada | Google Play |

## Dos datos técnicos que condicionan la solución

1. **Safari del iPad se presenta como una Mac.** Desde iPadOS 13, Safari manda un User-Agent de escritorio (`Macintosh; Intel Mac OS X…`). Mirando sólo el User-Agent en el servidor, un iPad es igual a una Mac. Por eso el iPad tiene que detectarse **en la página**, con JavaScript (pantalla táctil sobre un User-Agent de Mac). iPhone y iPod sí se distinguen por User-Agent.
2. **iOS no abre la app por una redirección automática.** Safari no abre un Universal Link si la navegación viene de una redirección o se queda dentro del mismo dominio, y el final del cambio de contraseña ocurre dentro de `backend.wind.do`. Sí abre la app cuando el usuario **toca** un enlace a su esquema propio (`windtv://open`); Safari pregunta antes "¿Abrir en WindTV?". Por eso en iOS hace falta una página con un botón, no un redirect.

## Qué hay que implementar en el backend

### 1. Archivos de verificación de dominio

Hacen que, al tocar un enlace de `backend.wind.do` desde otra app (el botón del correo, un QR escaneado con la cámara), el sistema abra la app directo, sin pasar por el navegador. Es la forma definitiva de abrir la app; la página del punto 2 cubre los casos donde el sistema no lo permite.

**iOS: `https://backend.wind.do/.well-known/apple-app-site-association`**

```json
{
  "applinks": {
    "details": [
      {
        "appIDs": ["TXH7LJ7B2S.com.windtelecom.windtv"],
        "components": [
          { "/": "/wind/go/windtv/*", "comment": "Ir a WindTV" },
          { "/": "/wind/l/v1/*", "comment": "QR para vincular una TV" }
        ]
      }
    ]
  }
}
```

**Android: `https://backend.wind.do/.well-known/assetlinks.json`**

```json
[
  {
    "relation": ["delegate_permission/common.handle_all_urls"],
    "target": {
      "namespace": "android_app",
      "package_name": "com.wind.android.streaming",
      "sha256_cert_fingerprints": ["<SHA-256 del certificado de firma de Play>"]
    }
  }
]
```

El SHA-256 está en Play Console → la app → Integridad de la app → Firma de apps ("Certificado de la clave de firma de apps"). Si hay más de uno (por ejemplo, el de subida), se ponen todos.

**Requisitos para que Apple y Google los acepten** (si falla alguno, la verificación falla sin avisar):
- Por HTTPS, en esa ruta exacta, **sin redirecciones**, con `Content-Type: application/json`.
- El de Apple va **sin extensión** y pesa menos de 128 KB.
- **La cadena SSL de `backend.wind.do` tiene que estar completa.** Es el pendiente de `docs/COMANDOS_CADENA_SSL_BACKEND_WIND_DO_2026-09-22.md` (faltaba el intermedio de GoDaddy): Apple y Google descargan estos archivos con validación estricta.
- No tiene que pasar por autenticación, CORS ni reescrituras de Django.

Ejemplo en nginx:

```nginx
location = /.well-known/apple-app-site-association {
    default_type application/json;
    alias /srv/wind/well-known/apple-app-site-association;
}
location = /.well-known/assetlinks.json {
    default_type application/json;
    alias /srv/wind/well-known/assetlinks.json;
}
```

### 2. `go_windtv_view`: identificar el dispositivo y actuar

- **Android** (el User-Agent contiene `android`, que cubre teléfonos y tablets): **sin cambios**. Se mantiene el `302` al `intent://` con fallback a Google Play, que ya funciona.
- **Todo lo demás**: en vez de redirigir a la web, responder una página chica que decide en el navegador:
  - **iPhone, iPad o iPod**: mostrar el botón "Abrir WindTV" (`windtv://open`) y los enlaces a la App Store (cuando exista) y a la web. Si al tocar el botón la app no está instalada, a los 1,5 s pasar a la App Store o, mientras no esté publicada, a la web.
  - **Computadora**: redirigir enseguida a la web de WindTV, como hoy.

```python
def go_windtv_view(request):
    ua = request.META.get("HTTP_USER_AGENT", "").lower()

    if "android" in ua:
        # Sin cambios: intent:// con fallback a Google Play (teléfonos y tablets).
        ...

    # iPhone, iPod, iPad (que se presenta como Mac) y computadoras: decide la página.
    return render(request, "wind/go_windtv.html", {
        "ios_app_url": f"{EmailConfig.WINDTV_IOS_SCHEME}://open",
        "app_store_url": EmailConfig.WINDTV_IOS_APP_STORE_URL,  # vacío hasta publicar
        "web_url": EmailConfig.WINDTV_WEB_URL,
    })
```

Plantilla `wind/go_windtv.html` (la parte que importa; el estilo, como el resto de las páginas de Wind):

```html
<main>
  <h1>WindTV</h1>
  <div id="ios" hidden>
    <a class="btn-primary" id="open-app" href="{{ ios_app_url }}">Abrir WindTV</a>
    {% if app_store_url %}
      <a href="{{ app_store_url }}">Descargar en la App Store</a>
    {% endif %}
    <a href="{{ web_url }}">Usar la versión web</a>
  </div>
  <noscript><a href="{{ web_url }}">Ir a WindTV</a></noscript>
</main>
<script>
  (function () {
    var ua = navigator.userAgent;
    // iPhone e iPod por User-Agent; el iPad (iPadOS 13+) se presenta como Mac,
    // se lo reconoce por la pantalla táctil.
    var isIOS = /iPhone|iPad|iPod/i.test(ua) ||
                (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
    if (!isIOS) {
      window.location.replace("{{ web_url }}");
      return;
    }
    document.getElementById("ios").hidden = false;
    // Si la app no está instalada, el esquema no abre nada y la página sigue visible:
    // a los 1,5 s se va a la App Store, o a la web mientras no esté publicada.
    document.getElementById("open-app").addEventListener("click", function () {
      setTimeout(function () {
        if (!document.hidden) {
          window.location.href = "{{ app_store_url|default:web_url }}";
        }
      }, 1500);
    });
  })();
</script>
```

**No** abrir `windtv://open` automáticamente al cargar la página: si la app no está instalada, Safari muestra un error ("la dirección no es válida").

### 3. Configuración nueva (`EmailConfig` o donde corresponda)

| Clave | Valor |
|---|---|
| `WINDTV_IOS_SCHEME` | `windtv` |
| `WINDTV_IOS_BUNDLE_ID` | `com.windtelecom.windtv` |
| `WINDTV_IOS_TEAM_ID` | `TXH7LJ7B2S` |
| `WINDTV_IOS_APP_STORE_URL` | Vacío hasta publicar; después, `https://apps.apple.com/app/id<ID>` |
| `WINDTV_ANDROID_PACKAGE` | `com.wind.android.streaming` (ya existe) |
| `WINDTV_ANDROID_SHA256` | El SHA-256 de firma de Play, para `assetlinks.json` |
| `WINDTV_WEB_URL` | `https://windtv.wind.do/` (ya existe) |

Aprovechando: `WIND_APP_GOOGLE_PLAY_URL` (correo de bienvenida) usa el paquete `com.wind.windtv`; el real es `com.wind.android.streaming`.

## Cómo queda cada caso

| Desde dónde | iPhone, iPad, iPod | Android (teléfono y tablet) | Computadora |
|---|---|---|---|
| Botón "Ir a WindTV" del correo, app instalada | Abre la app directo (Universal Link) | Abre la app directo (App Link) | Web de WindTV |
| Final del cambio de contraseña (`origin=app`) | Página con "Abrir WindTV" → Safari pregunta → abre la app | Abre la app (`intent://`) | Web de WindTV |
| Sin la app instalada | App Store (o web mientras no esté publicada) | Google Play | Web de WindTV |
| QR de vincular TV escaneado con la cámara | Abre la app en "Vincular TV" (cuando esté esa pantalla) | Igual | — |

## Cómo probarlo

1. Los archivos se sirven bien:
   - `curl -sI https://backend.wind.do/.well-known/apple-app-site-association` → `200`, `Content-Type: application/json`, sin `Location`.
   - `curl -sI https://backend.wind.do/.well-known/assetlinks.json` → idem.
   - Google tiene un verificador: `https://digitalassetlinks.googleapis.com/v1/statements:list?source.web.site=https://backend.wind.do&relation=delegate_permission/common.handle_all_urls`.
2. iPad y iPhone con la app instalada: desde el correo "Contraseña actualizada", tocar "Ir a WindTV" → se abre la app. Terminar un cambio de contraseña pedido desde la app → aparece "Abrir WindTV" → al tocarlo, Safari pregunta y se abre la app.
3. iPhone sin la app: la página, al tocar el botón, pasa a la web (o a la App Store cuando esté publicada).
4. Android teléfono y tablet: los mismos dos casos abren la app; sin la app, Google Play.
5. Computadora (Chrome, Safari de Mac, Firefox): va directo a la web de WindTV. **Ojo:** Safari de Mac no tiene pantalla táctil (`maxTouchPoints` = 0), así que no se confunde con un iPad.

Apple descarga su archivo cuando se instala o actualiza la app, así que para probar el punto 2 hay que reinstalarla después de publicar el archivo.

## Lo que ya queda hecho en las apps

- **iOS (WindTV):** registrado el esquema `windtv://open` y el dominio asociado `applinks:backend.wind.do`. Al abrirse por `windtv://open` o por `https://backend.wind.do/wind/go/windtv/…`, la app abre en el login o en Inicio. `https://backend.wind.do/wind/l/v1/{udid}/?t={temp_token}` queda reconocido para la pantalla de vincular TV.
- **Android (WindTV):** el esquema `windtv://open` ya existía. Se agregan los App Links verificados para `https://backend.wind.do/wind/go/windtv/…` y `/wind/l/v1/…`, con el mismo comportamiento que en iOS.

Mientras backend no publique los archivos del punto 1, los enlaces `https://` siguen abriéndose en el navegador (no rompe nada) y el esquema propio funciona igual.
