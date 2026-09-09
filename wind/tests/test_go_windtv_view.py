"""
Tests de `go_windtv_view` (redirector inteligente "Ir a WindTV", usado por
el correo de "contraseña actualizada" y, desde 2026-09-08, también por el
redirect final de reset-password.html). No toca la base de datos --
SimpleTestCase alcanza.

Cubre puntualmente el bug real encontrado el 2026-09-08: el caso Android
armaba el Location con HttpResponseRedirect, que valida el esquema contra
allowed_schemes = ["http", "https", "ftp"] y tira DisallowedRedirect para
cualquier otro -- "intent://" nunca iba a pasar esa validación (era
justo el hallazgo de logs "Unsafe redirect to URL with protocol 'intent'").
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

    def test_non_android_redirects_to_web(self):
        response = self.client.get(
            "/wind/go/windtv/",
            HTTP_USER_AGENT="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://windtv.wind.do/")
