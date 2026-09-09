"""
Nuevo flujo de eliminación de cuenta con confirmación por correo y
ejecución diferida a la fecha de corte de la suscripción (2026-09-08,
pedido del cliente: "si decide eliminar hoy pero le quedan 5 días de uso,
debería esperar esos 5 días para que se ejecute"). Ver
wind/services/account_deletion.py y wind.tasks.retry_partial_closures_task
(filtro nuevo por scheduled_closure_at).

CORREGIDO (2026-09-09): la primera versión de este flujo cortaba el acceso
(status PENDING_CLOSURE + User.is_active=False) apenas se confirmaba el
correo -- eso contradecía justo lo que pidió el cliente (seguir usando el
servicio hasta la fecha de corte). Ahora confirm_account_deletion() solo
programa scheduled_closure_at; el abonado sigue ACTIVE y usable hasta que
retry_partial_closures_task ejecuta el cierre real en esa fecha.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.signing import BadSignature, SignatureExpired
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from wind.models import AccountDeletionRequest, ListOfSubscriber, SubscriberEmailRegistry
from wind.services.account_deletion import (
    build_deletion_token,
    confirm_account_deletion,
    parse_deletion_token,
    request_account_deletion,
)

User = get_user_model()


class AccountDeletionTokenTestCase(TestCase):
    def test_round_trip(self):
        token = build_deletion_token(42, "CODE1")
        request_id, code = parse_deletion_token(token)
        self.assertEqual(request_id, 42)
        self.assertEqual(code, "CODE1")

    def test_tampered_token_is_rejected(self):
        token = build_deletion_token(42, "CODE1")
        with self.assertRaises(BadSignature):
            parse_deletion_token(token + "x")

    def test_expired_token_is_rejected(self):
        token = build_deletion_token(42, "CODE1")
        with patch("wind.services.account_deletion.ACCOUNT_DELETION_TOKEN_MAX_AGE_SECONDS", -1):
            with self.assertRaises(SignatureExpired):
                parse_deletion_token(token)


@patch("wind.services.account_deletion_email.enqueue_account_deletion_confirmation_email")
class RequestAccountDeletionTestCase(TestCase):
    def setUp(self):
        self.sub = ListOfSubscriber.objects.create(
            id="DEL1", code="DEL1", status=ListOfSubscriber.STATUS_ACTIVE, emails="user@example.com"
        )

    def test_creates_request_and_does_not_touch_subscriber(self, mock_enqueue):
        result = request_account_deletion("DEL1")

        self.assertTrue(result["success"])
        self.assertTrue(AccountDeletionRequest.objects.filter(subscriber_code="DEL1").exists())
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ListOfSubscriber.STATUS_ACTIVE)
        self.assertIsNone(self.sub.scheduled_closure_at)
        mock_enqueue.assert_called_once()

    def test_second_call_reuses_same_row_instead_of_duplicating(self, mock_enqueue):
        request_account_deletion("DEL1")
        request_account_deletion("DEL1")

        self.assertEqual(AccountDeletionRequest.objects.filter(subscriber_code="DEL1").count(), 1)
        self.assertEqual(mock_enqueue.call_count, 2)

    def test_rejects_when_already_closed(self, mock_enqueue):
        self.sub.status = ListOfSubscriber.STATUS_CLOSED
        self.sub.save(update_fields=["status"])

        result = request_account_deletion("DEL1")

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "already_closed")
        mock_enqueue.assert_not_called()

    def test_rejects_when_closure_already_scheduled(self, mock_enqueue):
        # CORREGIDO (2026-09-09): con el nuevo diseño, un cierre confirmado
        # y en espera de la fecha de corte sigue en status ACTIVE (no
        # cambia a PENDING_CLOSURE hasta que el cierre se ejecuta de
        # verdad) -- scheduled_closure_at es la señal de "ya hay una
        # eliminación en curso", no el status.
        self.sub.scheduled_closure_at = timezone.now() + timedelta(days=5)
        self.sub.save(update_fields=["scheduled_closure_at"])

        result = request_account_deletion("DEL1")

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "closure_already_scheduled")
        mock_enqueue.assert_not_called()

    def test_rejects_when_no_email_on_file(self, mock_enqueue):
        self.sub.emails = ""
        self.sub.save(update_fields=["emails"])

        result = request_account_deletion("DEL1")

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "no_email_on_file")
        mock_enqueue.assert_not_called()


class ConfirmAccountDeletionTestCase(TestCase):
    def setUp(self):
        self.expiry = timezone.now() + timedelta(days=5)
        self.sub = ListOfSubscriber.objects.create(
            id="DEL2",
            code="DEL2",
            status=ListOfSubscriber.STATUS_ACTIVE,
            emails="closeme@example.com",
            lastExpiryTime=self.expiry,
        )
        self.user = User.objects.create_user(
            username="closeme", email="closeme@example.com", password="Sup3rSecure!", is_active=True
        )
        SubscriberEmailRegistry.objects.create(email=self.user.email, subscriber_code="DEL2")

    def _make_confirmed_token(self):
        with patch("wind.services.account_deletion_email.enqueue_account_deletion_confirmation_email"):
            request_account_deletion("DEL2")
        req = AccountDeletionRequest.objects.get(subscriber_code="DEL2")
        return build_deletion_token(req.id, "DEL2"), req

    def test_confirm_schedules_cutoff_date_without_cutting_access(self):
        # CORREGIDO (2026-09-09): el cliente confirmó que el abonado debe
        # seguir usando el servicio con normalidad durante los días que le
        # queden ("le quedan 5 días de uso, debería esperar esos 5 días
        # para que se ejecute") -- confirmar el correo ya NO corta el
        # acceso ni cambia el status, solo programa la fecha de corte. El
        # corte real pasa recién cuando retry_partial_closures_task
        # ejecuta el cierre en esa fecha.
        token, req = self._make_confirmed_token()

        result = confirm_account_deletion(token)

        self.assertTrue(result["success"])
        self.assertFalse(result.get("already_confirmed"))

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ListOfSubscriber.STATUS_ACTIVE)
        self.assertEqual(self.sub.scheduled_closure_at, self.expiry)

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

        req.refresh_from_db()
        self.assertIsNotNone(req.confirmed_at)

    def test_confirm_is_idempotent_on_second_click(self):
        token, _req = self._make_confirmed_token()
        confirm_account_deletion(token)

        result = confirm_account_deletion(token)

        self.assertTrue(result["success"])
        self.assertTrue(result["already_confirmed"])

    def test_expired_token_returns_friendly_error(self):
        token, _req = self._make_confirmed_token()
        with patch("wind.services.account_deletion.ACCOUNT_DELETION_TOKEN_MAX_AGE_SECONDS", -1):
            result = confirm_account_deletion(token)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "TokenExpired")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ListOfSubscriber.STATUS_ACTIVE)

    def test_confirm_without_known_expiry_falls_back_to_now(self):
        # Sin lastExpiryTime sincronizado no hay una fecha de corte segura
        # para diferir -- CORREGIDO (2026-09-09): en vez de dejarlo
        # confirmado con scheduled_closure_at=None (que nunca dispararía
        # el cierre, porque el nuevo filtro de retry_partial_closures_task
        # exige scheduled_closure_at no nulo para las filas ACTIVE), se usa
        # "ahora" como fecha, para que el próximo ciclo del task lo agarre
        # y ejecute el cierre real (red de seguridad, se loguea para
        # revisar por qué faltaba el dato).
        self.sub.lastExpiryTime = None
        self.sub.save(update_fields=["lastExpiryTime"])
        token, _req = self._make_confirmed_token()

        before = timezone.now()
        result = confirm_account_deletion(token)

        self.assertTrue(result["success"])
        self.assertIsNotNone(result["scheduled_for"])
        self.sub.refresh_from_db()
        self.assertIsNotNone(self.sub.scheduled_closure_at)
        self.assertGreaterEqual(self.sub.scheduled_closure_at, before)
        self.assertEqual(self.sub.status, ListOfSubscriber.STATUS_ACTIVE)


class RetryPartialClosuresRespectsScheduleTestCase(TestCase):
    """
    Cobertura puntual del filtro nuevo agregado a retry_partial_closures_task
    -- el resto del comportamiento de esa task (conteo de reintentos, alerta
    al agotarlos) no es nuevo y no se vuelve a probar acá.
    """

    def setUp(self):
        self.future_sub = ListOfSubscriber.objects.create(
            id="FUT1",
            code="FUT1",
            status=ListOfSubscriber.STATUS_PENDING_CLOSURE,
            scheduled_closure_at=timezone.now() + timedelta(days=5),
        )
        self.due_sub = ListOfSubscriber.objects.create(
            id="DUE1",
            code="DUE1",
            status=ListOfSubscriber.STATUS_PENDING_CLOSURE,
            scheduled_closure_at=timezone.now() - timedelta(minutes=1),
        )
        self.legacy_sub = ListOfSubscriber.objects.create(
            id="LEG1",
            code="LEG1",
            status=ListOfSubscriber.STATUS_PENDING_CLOSURE,
            scheduled_closure_at=None,
        )
        # CORREGIDO (2026-09-09): estos tres son el caso nuevo -- una
        # eliminación confirmada (ver confirm_account_deletion) que
        # todavía no se ejecutó nunca sigue en status ACTIVE, no
        # PENDING_CLOSURE. active_due_sub es la PRIMERA ejecución real de
        # ese cierre; active_future_sub no debe tocarse todavía;
        # active_no_schedule_sub es un abonado activo normal, sin ninguna
        # eliminación en curso -- nunca debe entrar acá.
        self.active_due_sub = ListOfSubscriber.objects.create(
            id="ACTDUE1",
            code="ACTDUE1",
            status=ListOfSubscriber.STATUS_ACTIVE,
            scheduled_closure_at=timezone.now() - timedelta(minutes=1),
        )
        self.active_future_sub = ListOfSubscriber.objects.create(
            id="ACTFUT1",
            code="ACTFUT1",
            status=ListOfSubscriber.STATUS_ACTIVE,
            scheduled_closure_at=timezone.now() + timedelta(days=5),
        )
        self.active_no_schedule_sub = ListOfSubscriber.objects.create(
            id="ACTNORM1",
            code="ACTNORM1",
            status=ListOfSubscriber.STATUS_ACTIVE,
            scheduled_closure_at=None,
        )

    @patch("wind.services.subscriber_closure.close_subscriber_account")
    def test_skips_future_scheduled_but_runs_due_and_legacy(self, mock_close):
        mock_close.return_value = {"success": True}
        from wind.tasks import retry_partial_closures_task

        retry_partial_closures_task.run()

        called_codes = {call.args[0] for call in mock_close.call_args_list}
        self.assertNotIn("FUT1", called_codes)
        self.assertIn("DUE1", called_codes)
        self.assertIn("LEG1", called_codes)
        self.assertIn("ACTDUE1", called_codes)
        self.assertNotIn("ACTFUT1", called_codes)
        self.assertNotIn("ACTNORM1", called_codes)


class RequestAccountDeletionEndpointTestCase(APITestCase):
    URL = "/api/v1/profile/account/close/request/"

    def setUp(self):
        self.sub = ListOfSubscriber.objects.create(
            id="DELAPI1", code="DELAPI1", status=ListOfSubscriber.STATUS_ACTIVE, emails="api@example.com"
        )
        self.user = User.objects.create_user(
            username="apiuser", email="api@example.com", password="Sup3rSecure!"
        )
        SubscriberEmailRegistry.objects.create(email=self.user.email, subscriber_code="DELAPI1")
        self.client.force_authenticate(user=self.user)

    @patch("wind.services.account_deletion_email.enqueue_account_deletion_confirmation_email")
    def test_authenticated_owner_can_request_deletion(self, mock_enqueue):
        response = self.client.post(self.URL, {"code": "DELAPI1"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        self.assertTrue(AccountDeletionRequest.objects.filter(subscriber_code="DELAPI1").exists())

    def test_rejects_code_for_another_subscriber(self):
        response = self.client.post(self.URL, {"code": "SOMEONE_ELSE"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
