# Implementación: QR de pareo UDID como URL versionada (hallazgo #34)

Fecha: 2026-09-03
Referencia: `docs/PROPUESTA_FORMATO_QR_UDID_2026-09-02.md` (diseño y justificación completa), `docs/GUIA_INTEGRACION_UNIFICADA.md` (sección 1.1.1), `docs/PAREO_UDID_AUTOSERVICIO_CUENTA_2026-09-02.md` (destino final del link).

## Qué se implementó

### 1. Backend -- nueva ruta `/wind/l/v1/<udid>/`

`wind/views.py`, `link_device_view()`:

```python
_UDID_FORMAT_RE = re.compile(r'^[0-9a-fA-F]{1,32}$')

def link_device_view(request, udid):
    if not _UDID_FORMAT_RE.match(udid or ''):
        return HttpResponseRedirect('/wind/login/')
    dashboard_url = f"/wind/dashboard/?link_tv={quote(udid)}"
    return HttpResponseRedirect(dashboard_url)
```

Deliberadamente "boba": no toca `UDIDAuthRequest` ni la base de datos, solo sanea el formato (evita reflejar basura en la URL de redirect) y redirige. Toda la validación real (expiración, estado, conflicto de smartcard, rate limit) sigue viviendo en `AssociateUDIDByAccountView`, sin cambios -- este endpoint solo arma el camino para llegar ahí con el código precargado. Registrado en `wind/urls.py` bajo el mismo prefijo `wind/` que el resto de las rutas de pareo.

El parámetro `t` (`temp_token`) se acepta en la URL pero no se usa ni se reenvía en este camino -- queda ahí por si una futura app nativa con Universal/App Link configurado intercepta la URL antes de que llegue al navegador y lo necesita para su propio flujo (sección 2.2 de la guía unificada, login social + `udid`/`temp_token`). Ese camino no se implementó ni se puede implementar desde este repo (ver "Qué queda afuera").

### 2. `login.html` -- soporte de `next=` (saneado)

Antes, si `dashboard.html` detectaba que no había sesión, mandaba siempre a `/wind/login/` sin forma de volver a donde el usuario venía. Ahora:

```js
function safeNextUrl() {
  const next = params.get("next");
  if (!next) return null;
  if (!next.startsWith("/") || next.startsWith("//") || next.includes("://")) {
    return null;
  }
  return next;
}
function goDashboard() {
  window.location.href = safeNextUrl() || "{% url 'dashboard' %}";
}
```

El saneo es a propósito estricto: solo rutas relativas de un solo `/` inicial. `next=//evil.com/...` o `next=https://evil.com` se descartan (protocolo-relativo y URL absoluta son los dos trucos clásicos de open-redirect) y caen al dashboard de siempre.

### 3. `dashboard.html` -- preservar y precargar

- Si no hay sesión, ahora redirige a `/wind/login/?next=<ruta+query actual>` en vez de perder el destino.
- Al cargar la cuenta con éxito, `maybeHandleLinkTvParam()` lee `?link_tv=`, cambia a la pestaña "Vincular dispositivo", precarga el input, y limpia el query param de la URL (`history.replaceState`) para que un refresh no repita el salto de pestaña.

### 4. `appVideo` -- el QR ahora es la URL

`LoginPage.jsx`, `buildUdidQr()`: el payload pasa de `"{appName}:{udid}:{temp_token}"` a `"{baseUrl}/wind/l/v1/{udid}/?t={temp_token}"`, usando `effectiveUdidConfig.baseUrl` (ya resuelto por brand, `https://backend.wind.do` para `wind`). Si por algún motivo `baseUrl` viniera vacío (no debería pasar con `enabled: true`), cae al formato viejo en vez de generar una URL relativa sin sentido.

## Verificación hecha

- `py_compile` + `manage.py check` sobre los archivos de backend tocados -- limpio.
- `node --check` / build con `esbuild --jsx=automatic` sobre `LoginPage.jsx` -- sin errores de sintaxis.
- Templates `login.html` y `dashboard.html` renderizados de punta a punta con `get_template().render()` -- confirmado que `safeNextUrl`/`maybeHandleLinkTvParam` están en el HTML servido.
- 3 tests nuevos (`wind/tests/test_link_device_view.py`, `SimpleTestCase`, corridos contra Postgres real vía `pgserver` igual que el resto): `udid` válido → redirige con `link_tv` precargado; `udid` con caracteres fuera de lo esperado → Django lo resuelve como 404 antes de llegar a la vista (el patrón de URL ya lo filtra); `udid` de longitud excesiva → cae al login en vez de reflejarse. 3/3 OK.
- Probado manualmente con `RequestFactory` (sin servidor real) que el redirect exacto es `/wind/dashboard/?link_tv=<udid>` para un código válido.

## Qué queda afuera (no es negociable desde este repo)

- **Universal Link (iOS) / App Link (Android):** configurar `backend.wind.do` para que el sistema operativo abra la app nativa en vez del navegador al tocar este link es trabajo exclusivo del equipo que mantiene esas apps (`apple-app-site-association`, `assetlinks.json`, entitlements). Sin esto, la URL sigue funcionando -- simplemente abre siempre en navegador y termina en el auto-servicio web, nunca en el flujo nativo de login social. No hay nada que hacer desde Back-Wind-V2 ni `appVideo` para forzar esa parte.
- No se tocó `AuthenticateWithUDIDView`, `ValidateAndAssociateUDIDView`, `_maybe_authorize_tv_pairing`, `consumers.py` ni `udid_auth_service.py` -- ninguno de los flujos de asociación existentes cambió de comportamiento.
- El formato viejo (`"{appName}:{udid}:{temp_token}"`) ya no lo genera `appVideo`, pero si algún cliente viejo en caché o algún otro consumidor todavía lo esperara, dejó de recibirlo -- no hay período de transición con ambos formatos en paralelo. Se evaluó y se descartó mantenerlo porque no hay confirmación de que nadie lo haya consumido todavía (ver justificación en la propuesta original).

## Rollback

Si hace falta revertir: en `LoginPage.jsx`, volver el payload de `buildUdidQr` al string plano de antes (git revert del bloque). La ruta `/wind/l/v1/<udid>/` puede quedar viva sin problema aunque nada la use -- es inerte por sí sola (no la llama nada más).
