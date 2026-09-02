"""
Tests de `AssociateUDIDByAccountView` (pareo UDID auto-servicio desde el
dashboard web, sin `temp_token`; ver
docs/PAREO_UDID_AUTOSERVICIO_CUENTA_2026-09-02.md).

Cubre: autenticación obligatoria, resolución de subscriber/smartcard desde
el JWT (nunca desde el body), estados de UDIDAuthRequest (no encontrado,
expirado, no pendiente), conflicto de smartcard ya asociada, rate limit por
cuenta, y el camino feliz completo (incluida la notificación WS vía
`transaction.on_commit`, capturada explícitamente porque `TestCase` hace
rollback y esos callbacks no corren solos).
"""
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from wind.models import ListOfSmartcards, SubscriberInfo, UDIDAuthRequest

User = get_user_model()


class AssociateUDIDByAccountViewTestCase(APITestCase):
    url = '/wind/associate-udid-by-account/'

    def setUp(self):
        # LocMemCache (o Redis en producción) es un backend compartido a
        # nivel de proceso -- Django NO lo limpia solo entre tests, así que
        # sin esto el rate limit por cuenta (`check_udid_account_rate_limit`)
        # se va acumulando entre tests y contamina resultados que no tienen
        # nada que ver con probar el rate limit en sí.
        cache.clear()
        self.subscriber_code = 'BG$99001'
        self.user = User.objects.create_user(
            username='udidacct',
            email=f'{self.subscriber_code}@subscribers.wind.local',
            password='irrelevant-pass-123',
        )

    def _pending_request(self, **overrides):
        defaults = {'app_type': 'web', 'app_version': '1.0'}
        defaults.update(overrides)
        return UDIDAuthRequest.objects.create(**defaults)

    def test_anonymous_request_is_rejected(self):
        response = self.client.post(self.url, data={'udid': 'whatever'}, format='json')
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_authenticated_user_without_subscriber_gets_404(self):
        orphan = User.objects.create_user(
            username='nosub', email='nosub@example.com', password='irrelevant-pass-123'
        )
        self.client.force_authenticate(user=orphan)
        response = self.client.post(self.url, data={'udid': 'whatever'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unknown_udid_returns_400(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, data={'udid': 'deadbeef'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_expired_udid_is_marked_expired_and_rejected(self):
        req = self._pending_request()
        req.expires_at = timezone.now() - timedelta(minutes=1)
        req.save(update_fields=['expires_at'])

        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, data={'udid': req.udid}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        req.refresh_from_db()
        self.assertEqual(req.status, 'expired')

    def test_non_pending_udid_is_rejected(self):
        req = self._pending_request(status='used')

        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, data={'udid': req.udid}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_account_without_smartcard_returns_400(self):
        req = self._pending_request()

        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, data={'udid': req.udid}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_smartcard_already_linked_to_another_udid_is_a_conflict(self):
        ListOfSmartcards.objects.create(sn='SN001', subscriberCode=self.subscriber_code)
        SubscriberInfo.objects.create(sn='SN001', subscriber_code=self.subscriber_code)
        # Otro pareo ya usó esta misma smartcard.
        self._pending_request(status='used', sn='SN001', subscriber_code=self.subscriber_code)

        req = self._pending_request()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, data={'udid': req.udid}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('wind.views.check_udid_account_rate_limit', return_value=(False, 0, 900))
    def test_account_rate_limit_returns_429(self, mock_rate_limit):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, data={'udid': 'whatever'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data['error_code'], 'UDID_ACCOUNT_RATE_LIMIT_EXCEEDED')
        self.assertEqual(response['Retry-After'], '900')

    @patch('wind.views.async_to_sync')
    @patch('wind.views.get_channel_layer')
    def test_happy_path_associates_and_notifies_websocket_group(self, mock_get_channel_layer, mock_async_to_sync):
        ListOfSmartcards.objects.create(sn='SN777', subscriberCode=self.subscriber_code)
        SubscriberInfo.objects.create(sn='SN777', subscriber_code=self.subscriber_code)
        req = self._pending_request()

        mock_channel_layer = MagicMock()
        mock_get_channel_layer.return_value = mock_channel_layer
        # No nos interesa el comportamiento async real acá -- solo que la
        # función envuelta se invoque con los argumentos esperados.
        mock_async_to_sync.side_effect = lambda fn: fn

        self.client.force_authenticate(user=self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, data={'udid': req.udid}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['subscriber_code'], self.subscriber_code)
        self.assertEqual(response.data['smartcard_sn'], 'SN777')

        req.refresh_from_db()
        self.assertEqual(req.status, 'validated')
        self.assertEqual(req.subscriber_code, self.subscriber_code)
        self.assertEqual(req.sn, 'SN777')
        self.assertEqual(req.method, 'manual')
        self.assertTrue(req.validated_by_operator.startswith('account_self_service:'))

        mock_channel_layer.group_send.assert_called_once_with(
            f'udid_{req.udid}', {'type': 'udid.validated', 'udid': req.udid}
        )
