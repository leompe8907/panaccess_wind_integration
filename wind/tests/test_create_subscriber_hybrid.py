"""
Tests del modo "hybrid" de aprovisionamiento de suscriptores (Alto #3, ver
docs/APROVISIONAMIENTO_HIBRIDO_SUSCRIPTOR_2026-08-26.md). Punto 3 de "pendiente
antes de activar en producción": confirmar en la práctica que el corte por
presupuesto y el traspaso a background funcionan como se espera.

Mismo patrón de mocks que `wind.tests.test_auth.SubscriberRegistrationTestCase`
(PanAccess mockeado vía `wind.functions.create_subscriber.get_panaccess`).
`FeatureConfig` no es un setting de Django (es una clase con atributos
resueltos al importar `appConfig`), así que se parchea directo el atributo de
clase en vez de usar `override_settings`.
"""
import json
from unittest.mock import patch, MagicMock

from rest_framework import status
from rest_framework.test import APITestCase

from wind.models import SubscriberDocumentRegistry


class HybridProvisioningTestCase(APITestCase):
    def setUp(self):
        self.register_url = '/wind/create-subscriber/'
        self.valid_payload = {
            'firstName': 'Jane',
            'lastName': 'Roe',
            'email': 'jane.roe@example.com',
            'document_type': 'cedula',
            'document_number': '40298765432',
            'phone': '8095557890',
        }
        self.code_exists_patcher = patch(
            'wind.utils.subscriber_code_generator._code_exists_in_panaccess', return_value=False
        )
        self.code_exists_patcher.start()

    def tearDown(self):
        self.code_exists_patcher.stop()

    @patch('wind.functions.create_subscriber.release_registration_locks')
    @patch('wind.functions.create_subscriber.acquire_registration_locks', return_value=[object()])
    @patch('wind.utils.recaptcha.verify_recaptcha', return_value=(True, None))
    @patch('wind.functions.create_subscriber.FeatureConfig.CREATE_SUBSCRIBER_SYNC_BUDGET_SECONDS', 8)
    @patch('wind.functions.create_subscriber.FeatureConfig.CREATE_SUBSCRIBER_PROVISIONING_MODE', 'hybrid')
    @patch('wind.tasks.finish_subscriber_provisioning_task.delay')
    @patch('wind.functions.create_subscriber.get_panaccess')
    @patch('wind.functions.getSubscriber.get_panaccess')
    @patch('wind.services.welcome_email.enqueue_welcome_credentials_email')
    def test_hybrid_mode_completes_sync_when_within_budget(
        self, mock_welcome_email, mock_get_subscriber_panaccess, mock_get_panaccess, mock_task_delay,
        mock_verify_recaptcha, mock_acquire_locks, mock_release_locks,
    ):
        """Con presupuesto amplio (caso normal, PanAccess responde rápido -- todos
        los mocks son instantáneos), el modo hybrid debe comportarse exactamente
        como "sync": respuesta completa en el mismo request, sin encolar nada en
        background."""
        mock_client = MagicMock()
        mock_get_panaccess.return_value = mock_client
        mock_get_subscriber_panaccess.return_value = mock_client
        mock_client.call.side_effect = lambda method, params=None, timeout=60: {
            'addSubscriber': {'success': True, 'answer': '20001'},
            'addLicenseBlockToSubscriber': {'success': True, 'answer': True},
            'resetSubscriberPassword': {'success': True, 'answer': True},
            'getListOfExtendedSubscribers': {
                'success': True,
                'answer': {
                    'extendedSubscriberEntries': [
                        {
                            'subscriberCode': '20001',
                            'firstName': 'Jane',
                            'lastName': 'Roe',
                            'emails': ['jane.roe@example.com'],
                            'smartcards': ['123456789012347'],
                        }
                    ]
                },
            },
            'addProductToSmartcards': {'success': True, 'answer': True},
        }.get(method, {'success': False, 'errorMessage': f'Mock error for {method}'})

        response = self.client.post(
            self.register_url, data=json.dumps(self.valid_payload), content_type='application/json'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['success'])
        # Contrato síncrono completo -- exactamente lo que ya espera el
        # frontend hoy (modo "sync"), sin "provisioning_status: partial".
        self.assertIn('token', response.data)
        self.assertNotEqual(response.data.get('provisioning_status'), 'partial')
        self.assertTrue(SubscriberDocumentRegistry.objects.filter(document='40298765432').exists())
        mock_task_delay.assert_not_called()

    @patch('wind.functions.create_subscriber.release_registration_locks')
    @patch('wind.functions.create_subscriber.acquire_registration_locks', return_value=[object()])
    @patch('wind.utils.recaptcha.verify_recaptcha', return_value=(True, None))
    @patch('wind.functions.create_subscriber.FeatureConfig.CREATE_SUBSCRIBER_SYNC_BUDGET_SECONDS', 8)
    @patch('wind.functions.create_subscriber.FeatureConfig.CREATE_SUBSCRIBER_PROVISIONING_MODE', 'hybrid')
    @patch('wind.functions.create_subscriber.time.monotonic')
    @patch('wind.tasks.finish_subscriber_provisioning_task.delay')
    @patch('wind.functions.create_subscriber.get_panaccess')
    def test_hybrid_mode_hands_off_to_background_when_budget_exceeded(
        self, mock_get_panaccess, mock_task_delay, mock_monotonic, mock_verify_recaptcha,
        mock_acquire_locks, mock_release_locks,
    ):
        """Simula que PanAccess fue lento: `addSubscriber` ya se ejecutó (el
        suscriptor existe), pero para cuando se llega al primer checkpoint
        (justo antes de la búsqueda/lookup) el presupuesto ya se agotó --
        `time.monotonic()` se mockea para que la segunda lectura (el chequeo
        del deadline) ya esté por delante de la primera (el cálculo del
        deadline), sin necesidad de un `sleep()` real ni tests lentos/flaky."""
        mock_client = MagicMock()
        mock_get_panaccess.return_value = mock_client
        mock_client.call.side_effect = lambda method, params=None, timeout=60: {
            'addSubscriber': {'success': True, 'answer': '20002'},
        }.get(method, {'success': False, 'errorMessage': f'Mock error for {method}'})

        # 1a llamada: cálculo de sync_deadline (t=0 + budget). 2a llamada: el
        # chequeo "before_lookup" (t=100, muy por delante del deadline).
        mock_monotonic.side_effect = [0, 100]

        payload = dict(self.valid_payload, document_number='40298765433', email='jane.roe2@example.com')
        response = self.client.post(self.register_url, data=json.dumps(payload), content_type='application/json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['success'])
        self.assertEqual(response.data.get('provisioning_status'), 'partial')
        self.assertEqual(response.data.get('subscriber_code'), '20002')
        # Sin campos del contrato síncrono -- coordinado con las apps antes
        # de activar (ver doc referenciado en el docstring del módulo).
        self.assertNotIn('token', response.data)

        mock_task_delay.assert_called_once()
        self.assertEqual(mock_task_delay.call_args.kwargs['subscriber_code'], '20002')
