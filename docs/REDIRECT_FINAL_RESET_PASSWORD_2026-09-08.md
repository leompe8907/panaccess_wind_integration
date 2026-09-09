# Redirect final de "olvidé mi contraseña" + fix de bug real encontrado en el camino

Fecha: 2026-09-08

## Qué se pidió

Que al terminar el flujo de reset de contraseña, en vez de caer en `/wind/login/` (página de test del backend), lleve al sitio real -- y en Android/iOS, a la app correspondiente.

## Qué se hizo

`wind/templates/wind/reset-password.html` ahora redirige a `{% url 'go_windtv' %}` (`/wind/go/windtv/`) en vez de `/wind/login/`. Ese redirector **ya existía** (`wind.views.go_windtv_view`, usado por el botón "Ir a WindTV" del correo de "contraseña actualizada"): en Android intenta abrir la app instalada y cae a Play Store si no está; en el resto de los casos (incluido iOS, que todavía no tiene app publicada) va a `EmailConfig.WINDTV_WEB_URL` (`https://windtv.wind.do/` por defecto). No hizo falta lógica de marca -- este backend es exclusivo de Wind.

## Bug real encontrado y arreglado de paso

`go_windtv_view` arma la URL de Android con el esquema `intent://` y la devolvía con `HttpResponseRedirect`, que valida el esquema del `Location` contra `allowed_schemes = ["http", "https", "ftp"]` y rechaza cualquier otro con `DisallowedRedirect`. **`intent://` nunca iba a pasar esa validación** -- este es exactamente el hallazgo de logs de esta semana "Unsafe redirect to URL with protocol 'intent'" que había quedado flageado sin investigar por falta de logs frescos en ese momento. En la práctica, cualquier usuario Android que tocara ese link (el de "Ir a WindTV" del correo, y ahora también el nuevo redirect de reset) recibía un 500 en vez de abrir la app o caer al fallback de Play Store.

Se arma la respuesta a mano con `HttpResponse(status=302)` + header `Location` manual, que no pasa por esa validación de esquema. El fallback `S.browser_fallback_url` (a Play Store si la app no está instalada) sigue intacto.

## Archivos tocados

- `wind/views.py` (`go_windtv_view`, import de `HttpResponse`).
- `wind/templates/wind/reset-password.html`.

## Cómo se verificó

`py_compile` sobre `wind/views.py`. Se agregó `wind/tests/test_go_windtv_view.py` (no tenía cobertura antes): confirma que Android recibe el redirect `intent://` sin que Django tire `DisallowedRedirect`, y que el resto de plataformas sigue yendo a `WINDTV_WEB_URL`. 2/2 OK contra Postgres real.
