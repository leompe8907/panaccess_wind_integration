"""
Regresión del incidente de producción (2026-09-07): los prefijos de
subscriber_code (MANUAL_CODE_PREFIX, SOCIAL_PROVIDER_CODE_PREFIXES,
DEFAULT_SOCIAL_CODE_PREFIX, MANUAL_AUTO_CODE_PREFIX) llevaban un "$"
(p. ej. "BM$") que PanAccess rechaza en su campo `code` (solo acepta
a-z, A-Z, 0-9) -- eso rompía TODO registro nuevo (manual con documento,
manual sin documento, y login social vía `_create_subscriber_core`) desde
que se desplegó el esquema de prefijos, con el backend devolviendo 500
("No pudimos completar tu registro...") en el 100% de los intentos, sin que
nada lo hubiera detectado hasta que se revisaron los logs de diagnóstico.
Ver docs/FIX_PREFIJOS_SUBSCRIBER_CODE_2026-09-07.md.

Dos niveles de cobertura:
1. Los 4 constantes de prefijo son alfanuméricos puros (rápido, directo).
2. El valor real que se manda a PanAccess (`subscriber[code]` en la llamada
   a `addSubscriber`) también lo es, para los 3 orígenes de registro
   (manual con documento, manual sin documento, social).
"""
import json
import re
from unittest.mock import patch, MagicMock

from rest_framework import status
from rest_framework.test import APITestCase

from wind.functions.create_subscriber import (
    MANUAL_CODE_PREFIX,
    MANUAL_AUTO_CODE_PREFIX,
    SOCIAL_PROVIDER_CODE_PREFIXES,
    DEFAULT_SOCIAL_CODE_PREFIX,
    _create_subscriber_core,
)

_ALPHANUMERIC_RE = re.compile(r'^[a-zA-Z0-9]+$')


class SubscriberCodePrefixConstantsTestCase(APITestCase):
    def test_manual_prefix_is_alphanumeric(self):
        self.assertRegex(MANUAL_CODE_PREFIX, _ALPHANUMERIC_RE)

    def test_manual_auto_prefix_is_alphanumeric(self):
        self.assertRegex(MANUAL_AUTO_CODE_PREFIX, _ALPHANUMERIC_RE)

    def test_default_social_prefix_is_alphanumeric(self):
        self.assertRegex(DEFAULT_SOCIAL_CODE_PREFIX, _ALPHANUMERIC_RE)

    def test_all_social_provider_prefixes_are_alphanumeric(self):
        self.assertTrue(SOCIAL_PROVIDER_CODE_PREFIXES, "no debería estar vacío")
        for provider, prefix in SOCIAL_PROVIDER_CODE_PREFIXES.items():
            self.assertRegex(prefix, _ALPHANUMERIC_RE, f"prefijo de '{provider}' no es alfanumérico: {prefix!r}")


class SubscriberCodeSentToPanaccessIsAlphanumericTestCase(APITestCase):
    """
    No alcanza con que los prefijos en sí sean alfanuméricos -- hay que
    confirmar que el código final (prefijo + documento/progresivo) que
    efectivamente viaja a PanAccess también lo es, para los 3 orígenes.
    """

    def setUp(self):
        self.code_exists_patcher = patch(
            'wind.utils.subscriber_code_generator._code_exists_in_panaccess', return_value=False
        )
        self.code_exists_patcher.start()
        self.addCleanup(self.code_exists_patcher.stop)

        self.recaptcha_patcher = patch('wind.utils.recaptcha.verify_recaptcha', return_value=(True, None))
        self.recaptcha_patcher.start()
        self.addCleanup(self.recaptcha_patcher.stop)

        self.locks_patcher = patch(
            'wind.functions.create_subscriber.acquire_registration_locks', return_value=[object()]
        )
        self.locks_patcher.start()
        self.addCleanup(self.locks_patcher.stop)

        self.release_locks_patcher = patch('wind.functions.create_subscriber.release_registration_locks')
        self.release_locks_patcher.start()
        self.addCleanup(self.release_locks_patcher.stop)

        self.welcome_email_patcher = patch('wind.services.welcome_email.enqueue_welcome_credentials_email')
        self.welcome_email_patcher.start()
        self.addCleanup(self.welcome_email_patcher.stop)

    def _mock_panaccess(self, mock_get_panaccess, sent_codes):
        mock_client = MagicMock()
        mock_get_panaccess.return_value = mock_client

        def _side_effect(method, params=None, timeout=60):
            if method == 'addSubscriber':
                sent_codes.append(params.get('subscriber[code]'))
                return {'success': True, 'answer': '30001'}
            if method == 'getListOfExtendedSubscribers':
                return {
                    'success': True,
                    'answer': {
                        'extendedSubscriberEntries': [
                            {
                                'subscriberCode': '30001',
                                'firstName': 'Jane',
                                'lastName': 'Roe',
                                'emails': ['jane.roe.prefix@example.com'],
                                'smartcards': ['123456789012347'],
                            }
                        ]
                    },
                }
            return {'success': False, 'errorMessage': f'Mock error for {method}'}

        mock_client.call.side_effect = _side_effect
        return mock_client

    @patch('wind.functions.getSubscriber.get_panaccess')
    @patch('wind.functions.create_subscriber.get_panaccess')
    def test_manual_registration_with_document_sends_alphanumeric_code(
        self, mock_get_panaccess, mock_get_subscriber_panaccess,
    ):
        sent_codes = []
        mock_client = self._mock_panaccess(mock_get_panaccess, sent_codes)
        mock_get_subscriber_panaccess.return_value = mock_client

        payload = {
            'firstName': 'Jane',
            'lastName': 'Roe',
            'email': 'jane.roe.prefix@example.com',
            'document_type': 'cedula',
            'document_number': '40298765499',
            'phone': '8095557891',
        }
        response = self.client.post(
            '/wind/create-subscriber/', data=json.dumps(payload), content_type='application/json'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(len(sent_codes), 1)
        self.assertRegex(sent_codes[0], _ALPHANUMERIC_RE, f"code enviado a PanAccess no es alfanumérico: {sent_codes[0]!r}")
        self.assertTrue(sent_codes[0].startswith(MANUAL_CODE_PREFIX))

    @patch('wind.functions.getSubscriber.get_panaccess')
    @patch('wind.functions.create_subscriber.get_panaccess')
    def test_manual_registration_without_document_sends_alphanumeric_code(
        self, mock_get_panaccess, mock_get_subscriber_panaccess,
    ):
        sent_codes = []
        mock_client = self._mock_panaccess(mock_get_panaccess, sent_codes)
        mock_get_subscriber_panaccess.return_value = mock_client

        payload = {
            'firstName': 'John',
            'lastName': 'Doe',
            'email': 'john.doe.prefix@example.com',
            'phone': '8095557892',
        }
        response = self.client.post(
            '/wind/create-subscriber/', data=json.dumps(payload), content_type='application/json'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(len(sent_codes), 1)
        self.assertRegex(sent_codes[0], _ALPHANUMERIC_RE, f"code enviado a PanAccess no es alfanumérico: {sent_codes[0]!r}")
        self.assertTrue(sent_codes[0].startswith(MANUAL_AUTO_CODE_PREFIX))

    @patch('wind.functions.getSubscriber.get_panaccess')
    @patch('wind.functions.create_subscriber.get_panaccess')
    def test_social_registration_sends_alphanumeric_code(
        self, mock_get_panaccess, mock_get_subscriber_panaccess,
    ):
        # Mismo camino que usa `social_login_provisioning.create_subscriber_in_panaccess`
        # -- llama a `_create_subscriber_core` directo, sin pasar por la vista
        # HTTP pública (ver docstring de la función).
        sent_codes = []
        mock_client = self._mock_panaccess(mock_get_panaccess, sent_codes)
        mock_get_subscriber_panaccess.return_value = mock_client

        data = {
            'firstName': 'Social',
            'lastName': 'User',
            'email': 'social.user.prefix@example.com',
        }
        response = _create_subscriber_core(data, is_social_account=True, social_provider='google')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(len(sent_codes), 1)
        self.assertRegex(sent_codes[0], _ALPHANUMERIC_RE, f"code enviado a PanAccess no es alfanumérico: {sent_codes[0]!r}")
        self.assertTrue(sent_codes[0].startswith(SOCIAL_PROVIDER_CODE_PREFIXES['google']))
