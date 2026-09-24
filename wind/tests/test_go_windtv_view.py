"""
Tests de `go_windtv_view` (redirector inteligente "Ir a WindTV", usado por
el correo de "contraseña actualizada" y, desde 2026-09-08, también por el
redirect final de reset-password.html). No toca la base de datos --
SimpleTestCase alcanza.

Cubre el bug real encontrado el 2026-09-08 (Android armaba el Location con
HttpResponseRedirect, que rechaza el esquema "intent://" con
DisallowedRedirect) y el reportado el 2026-09-24 por el equipo de
iOS/Android (docs/ABRIR_APP_DESDE_BACKEND_WIND.md): iPhone/iPad caían
directo a la web en vez de intentar abrir la app -- acá la distinción real
iPad-vs-Mac no se puede probar desde el servidor (ambos comparten
User-Agent, ver el docstring de la vista), así que estos tests solo
verifican que el servidor manda a TODO lo que no sea Android a la página
intermedia, con el contexto correcto -- la decisión final iOS-vs-desktop
la hace el JS del lado del cliente (fuera del alcance de un test de
Django).
"""
from django.test import SimpleTestCase


class GoWindtvViewTestCase(SimpleTestCase):
    def test_android_gets_intent_redirect_without_raising_disallowed_redirect(self):
        response = self.client.get(
            "/wind/go/windtv/",
            HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120 Mobile",
        )

        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("intent://"))
        self.assertIn("package=com.wind.android.streaming", location)
        self.assertIn("S.browser_fallback_url=", location)

    def test_iphone_renders_intermediate_page_not_a_redirect(self):
        response = self.client.get(
            "/wind/go/windtv/",
            HTTP_USER_AGENT=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "wind/go_windtv.html")
        self.assertEqual(response.context["ios_app_url"], "windtv://open")
        self.assertEqual(response.context["web_url"], "https://windtv.wind.do/")
        self.assertContains(response, "windtv://open")

    def test_macos_desktop_ua_also_renders_intermediate_page(self):
        """
        El servidor no puede distinguir un iPad (iPadOS 13+, UA de
        escritorio) de una Mac real -- por diseño, cualquier UA que no sea
        Android llega a la misma página intermedia, y es el JS del lado
        del cliente (maxTouchPoints) el que manda a la web sin que el
        usuario vea nada si de verdad es una computadora. Este test solo
        confirma que el servidor no vuelve a redirigir directo como antes.
        """
        response = self.client.get(
            "/wind/go/windtv/",
            HTTP_USER_AGENT="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "wind/go_windtv.html")

    def test_intermediate_page_omits_app_store_link_when_not_configured(self):
        response = self.client.get(
            "/wind/go/windtv/",
            HTTP_USER_AGENT="Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X)",
        )

        self.assertEqual(response.context["app_store_url"], "")
        self.assertNotContains(response, "Descargar en la App Store")
