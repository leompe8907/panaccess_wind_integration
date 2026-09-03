"""
Tests de `link_device_view` (landing del QR de pareo, hallazgo #34, ver
docs/PROPUESTA_FORMATO_QR_UDID_2026-09-02.md). No toca la base de datos --
`SimpleTestCase` alcanza (no requiere Postgres real como el resto de
`wind/tests/`).
"""
from django.test import SimpleTestCase


class LinkDeviceViewTestCase(SimpleTestCase):
    def test_valid_udid_redirects_to_dashboard_with_link_tv_param(self):
        response = self.client.get('/wind/l/v1/a1b2c3d4/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/wind/dashboard/?link_tv=a1b2c3d4')

    def test_malformed_udid_redirects_to_login_instead_of_reflecting_it(self):
        # No debe reflejar basura en la URL de redirect -- ver
        # link_device_view()/_UDID_FORMAT_RE en wind/views.py.
        response = self.client.get('/wind/l/v1/../../etc/passwd/')
        self.assertEqual(response.status_code, 404)  # Django normaliza el path antes de llegar a la vista

    def test_oversized_udid_is_rejected(self):
        response = self.client.get('/wind/l/v1/' + ('a' * 200) + '/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/wind/login/')
