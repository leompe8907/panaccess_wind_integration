import json
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from wind.models import SubscriberEmailRegistry, SubscriberDocumentRegistry, ListOfSubscriber, SubscriberLoginInfo
from wind.services.subscriber_auth import get_or_create_portal_user
from wind.utils.encryption import encrypt_value

User = get_user_model()


class SubscriberRegistrationTestCase(APITestCase):
    def setUp(self):
        self.register_url = reverse('create_subscriber')
        self.valid_payload = {
            'firstName': 'John',
            'lastName': 'Doe',
            'email': 'john.doe@example.com',
            'document_type': 'cedula',
            'document_number': '40212345678',
            'phone': '8095551234'
        }
        # Mock global de _code_exists_in_panaccess
        self.code_exists_patcher = patch('wind.utils.subscriber_code_generator._code_exists_in_panaccess', return_value=False)
        self.code_exists_patcher.start()

    def tearDown(self):
        self.code_exists_patcher.stop()

    @patch('wind.functions.create_subscriber.get_panaccess')
    @patch('wind.functions.getSubscriber.get_panaccess')
    @patch('wind.services.welcome_email.enqueue_welcome_credentials_email')
    def test_successful_registration(self, mock_welcome_email, mock_get_subscriber_panaccess, mock_get_panaccess):
        # Configurar mocks de PanAccess
        mock_client = MagicMock()
        mock_get_panaccess.return_value = mock_client
        mock_get_subscriber_panaccess.return_value = mock_client
        
        # Mocks para llamadas consecutivas en create_subscriber
        # 1. addSubscriber (creación) -> responde success
        # 2. addLicenseBlockToSubscriber -> responde success
        # 3. getSubscriberLoginInfo (se hace en la vista de credentials o flujo interno)
        mock_client.call.side_effect = lambda method, params=None, timeout=60: {
            'addSubscriber': {'success': True, 'answer': '10001'},
            'addLicenseBlockToSubscriber': {'success': True, 'answer': True},
            'resetSubscriberPassword': {'success': True, 'answer': True},
            'getListOfExtendedSubscribers': {
                'success': True,
                'answer': {
                    'extendedSubscriberEntries': [
                        {
                            'subscriberCode': '10001',
                            'firstName': 'John',
                            'lastName': 'Doe',
                            'emails': ['john.doe@example.com'],
                            'smartcards': ['123456789012345', '123456789012346']
                        }
                    ]
                }
            },
            'addProductToSmartcards': {'success': True, 'answer': True}
        }.get(method, {'success': False, 'errorMessage': f'Mock error for {method}'})

        response = self.client.post(
            self.register_url,
            data=json.dumps(self.valid_payload),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['success'])
        self.assertIn('token', response.data)

        # Verificar que se crearon registros de unicidad local
        self.assertTrue(SubscriberEmailRegistry.objects.filter(email='john.doe@example.com').exists())
        self.assertTrue(SubscriberDocumentRegistry.objects.filter(document='40212345678').exists())
        mock_welcome_email.assert_called_once()
        call_kwargs = mock_welcome_email.call_args.kwargs
        self.assertEqual(call_kwargs["email"], "john.doe@example.com")
        self.assertEqual(call_kwargs["subscriber_code"], "10001")
        self.assertFalse(call_kwargs["is_social_account"])

    @patch('wind.functions.create_subscriber.get_panaccess')
    def test_duplicate_email_validation(self, mock_get_panaccess):
        # Crear un registro de email previo
        SubscriberEmailRegistry.objects.create(
            email='duplicate@example.com',
            subscriber_code='WND0001'
        )

        payload = self.valid_payload.copy()
        payload['email'] = 'duplicate@example.com'

        response = self.client.post(
            self.register_url,
            data=json.dumps(payload),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data.get('success', True))
        self.assertIn('email', response.data['errors'])

    @patch('wind.functions.create_subscriber.get_panaccess')
    def test_duplicate_document_validation(self, mock_get_panaccess):
        # Crear un registro de documento previo
        SubscriberDocumentRegistry.objects.create(
            document='40200000000',
            subscriber_code='WND0002'
        )

        payload = self.valid_payload.copy()
        payload['document_number'] = '40200000000'

        response = self.client.post(
            self.register_url,
            data=json.dumps(payload),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data.get('success', True))
        self.assertIn('document_number', response.data['errors'])


class SubscriberAuthTestCase(APITestCase):
    def setUp(self):
        self.login_url = reverse('token_obtain_pair') if hasattr(self, 'token_obtain_pair') else '/api/auth/login/'
        self.username = 'testuser'
        self.password = 'SuperSecurePass123!'
        self.email = 'testuser@example.com'
        self.user = User.objects.create_user(
            username=self.username,
            email=self.email,
            password=self.password
        )
        # Asociar suscriptor local
        SubscriberEmailRegistry.objects.create(
            email=self.email,
            subscriber_code='WND0003'
        )
        try:
            from allauth.account.models import EmailAddress

            EmailAddress.objects.create(
                user=self.user,
                email=self.email,
                verified=True,
                primary=True,
            )
        except ImportError:
            pass

    def test_get_or_create_portal_user_marks_email_verified(self):
        from allauth.account.models import EmailAddress

        SubscriberEmailRegistry.objects.create(email='pan@example.com', subscriber_code='CODE1')
        login_record = SubscriberLoginInfo.objects.create(
            subscriberCode='CODE1',
            login1=12345,
            login2='wtl@CODE1',
            password_hash=encrypt_value('secret'),
        )

        user = get_or_create_portal_user(login_record)

        addr = EmailAddress.objects.get(user=user, email='pan@example.com')
        self.assertTrue(addr.verified)
        self.assertTrue(addr.primary)

    def test_jwt_login_success(self):
        # Intentar login estándar a través de dj_rest_auth
        response = self.client.post(
            '/api/auth/login/',
            data={
                'username': self.email,
                'password': self.password
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)


class ClosedSubscriberLoginTestCase(APITestCase):
    """
    Auditoría, sección 17/21: cerrar la cuenta no debe dejar seguir
    logueando por credenciales de PanAccess (cacheadas o re-consultadas en
    vivo), ni reactivar el User que el cierre desactivó.
    """

    def _make_login_record(self, code, password='secret'):
        return SubscriberLoginInfo.objects.create(
            subscriberCode=code,
            login1=99001,
            login2=f'wtl@{code}',
            password_hash=encrypt_value(password),
        )

    def test_authenticate_portal_user_rejects_closed_subscriber(self):
        from wind.services.subscriber_auth import authenticate_portal_user

        ListOfSubscriber.objects.create(id='CLOSEDCODE', code='CLOSEDCODE', status=ListOfSubscriber.STATUS_CLOSED)
        self._make_login_record('CLOSEDCODE')

        user = authenticate_portal_user('99001', 'secret')

        self.assertIsNone(user)

    def test_authenticate_portal_user_rejects_pending_closure_subscriber(self):
        from wind.services.subscriber_auth import authenticate_portal_user

        ListOfSubscriber.objects.create(
            id='PENDCODE', code='PENDCODE', status=ListOfSubscriber.STATUS_PENDING_CLOSURE
        )
        self._make_login_record('PENDCODE', password='otrasecreta')

        user = authenticate_portal_user('99001', 'otrasecreta')

        self.assertIsNone(user)

    def test_authenticate_portal_user_allows_active_subscriber(self):
        from wind.services.subscriber_auth import authenticate_portal_user

        ListOfSubscriber.objects.create(id='ACTIVECODE', code='ACTIVECODE', status=ListOfSubscriber.STATUS_ACTIVE)
        self._make_login_record('ACTIVECODE', password='activa123')

        user = authenticate_portal_user('99001', 'activa123')

        self.assertIsNotNone(user)

    def test_get_or_create_portal_user_does_not_reactivate_closed_subscriber(self):
        ListOfSubscriber.objects.create(id='CLOSEDCODE2', code='CLOSEDCODE2', status=ListOfSubscriber.STATUS_CLOSED)
        login_record = self._make_login_record('CLOSEDCODE2', password='cualquiera')

        user = get_or_create_portal_user(login_record)

        self.assertFalse(user.is_active)
