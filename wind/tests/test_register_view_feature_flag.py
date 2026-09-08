"""
`register_view` (`GET /wind/register/`, la página HTML de registro público)
antes se renderizaba siempre, sin importar `FeatureConfig.CREATE_SUBSCRIBER_PUBLIC_ENABLED`
-- ese flag solo gateaba el POST del endpoint (`create_subscriber_view`), no
la página en sí. A pedido explícito del cliente: si la funcionalidad está
desactivada, la página debe dar 404 en vez de mostrar un formulario que
siempre va a fallar al enviarse.
"""
from unittest.mock import patch

from django.test import TestCase


class RegisterViewFeatureFlagTestCase(TestCase):
    @patch('wind.functions.create_subscriber._create_subscriber_public_enabled', return_value=True)
    def test_register_page_renders_when_enabled(self, _mock_flag):
        response = self.client.get('/wind/register/')
        self.assertEqual(response.status_code, 200)

    @patch('wind.functions.create_subscriber._create_subscriber_public_enabled', return_value=False)
    def test_register_page_404s_when_disabled(self, _mock_flag):
        response = self.client.get('/wind/register/')
        self.assertEqual(response.status_code, 404)
