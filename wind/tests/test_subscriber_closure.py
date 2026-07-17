"""
Tests de wind.services.subscriber_closure.

Cubre el bug encontrado en una prueba real: cerrar un suscriptor que nunca
habia sido sincronizado localmente (no existe fila en ListOfSubscriber) debia
dejar un tombstone (status=closed) para que el siguiente sync periodico no lo
vuelva a insertar como "active" con datos de PanAccess. Antes, el camino sin
fila previa hacia un `.filter(...).update(...)` sobre cero filas, que no crea
nada -- este test confirma que ahora si se crea la fila cerrada.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from wind.models import ListOfSubscriber, SubscriberEmailRegistry
from wind.services.subscriber_closure import close_subscriber_account

User = get_user_model()


class CloseSubscriberAccountNoLocalRowTestCase(TestCase):
    """El suscriptor no existe en ListOfSubscriber antes del cierre."""

    @patch("wind.services.subscriber_closure.deprovision_subscriber_in_panaccess")
    def test_creates_tombstone_when_no_local_row_existed(self, mock_deprovision):
        code = "40215440527"
        self.assertFalse(ListOfSubscriber.objects.filter(code=code).exists())

        mock_deprovision.return_value = {
            "dry_run": False,
            "subscriber_code": code,
            "smartcards_processed": 0,
            "smartcards_removed": 0,
            "subscriber_deleted": True,
            "steps": [],
            "warnings": [],
            "errors": [],
            "success": True,
        }

        result = close_subscriber_account(code, reason="prueba")

        self.assertTrue(result["success"], result)

        subscriber = ListOfSubscriber.objects.filter(code=code).first()
        self.assertIsNotNone(subscriber, "El cierre debe crear un tombstone local aunque no existiera fila previa")
        self.assertEqual(subscriber.status, ListOfSubscriber.STATUS_CLOSED)
        self.assertIsNotNone(subscriber.closed_at)
        self.assertEqual(subscriber.smartcards, [])

    @patch("wind.services.subscriber_closure.deprovision_subscriber_in_panaccess")
    def test_updates_existing_row_in_place(self, mock_deprovision):
        code = "1120743001"
        ListOfSubscriber.objects.create(
            id=code,
            code=code,
            firstName="Bromteck",
            lastName="Comercial",
            smartcards=["4001823852"],
            status=ListOfSubscriber.STATUS_ACTIVE,
        )

        mock_deprovision.return_value = {
            "dry_run": False,
            "subscriber_code": code,
            "smartcards_processed": 0,
            "smartcards_removed": 0,
            "subscriber_deleted": True,
            "steps": [],
            "warnings": [],
            "errors": [],
            "success": True,
        }

        result = close_subscriber_account(code, reason="prueba")

        self.assertTrue(result["success"], result)

        subscriber = ListOfSubscriber.objects.get(code=code)
        self.assertEqual(subscriber.status, ListOfSubscriber.STATUS_CLOSED)
        self.assertEqual(subscriber.smartcards, [])
        self.assertEqual(ListOfSubscriber.objects.filter(code=code).count(), 1)


class CloseSubscriberAccountDeactivatesPortalUserTestCase(TestCase):
    """
    Auditoría, sección 17/21/22: confirmado en producción que, tras cerrar
    una cuenta, una sesión ya logueada seguía entrando al dashboard con
    normalidad. La causa: `_deactivate_portal_users` solo corría después de
    un cierre 100% exitoso en PanAccess -- si la desaprovisión fallaba o
    quedaba parcial, el `User` de Django nunca se desactivaba. Estos tests
    confirman que ahora el usuario se desactiva de una vez al iniciar el
    cierre, sin importar el resultado final en PanAccess.
    """

    def _make_user_and_registry(self, code, email):
        user = User.objects.create_user(username=email, email=email, password="Whatever123!")
        SubscriberEmailRegistry.objects.create(email=email, subscriber_code=code)
        return user

    @patch("wind.services.subscriber_closure.deprovision_subscriber_in_panaccess")
    def test_deactivates_user_even_when_panaccess_fails(self, mock_deprovision):
        code = "40219990001"
        email = "cliente.cerrado@example.com"
        user = self._make_user_and_registry(code, email)
        self.assertTrue(user.is_active)

        mock_deprovision.return_value = {"success": False, "errors": ["timeout"], "steps": []}

        result = close_subscriber_account(code, reason="prueba")

        self.assertFalse(result["success"])
        user.refresh_from_db()
        self.assertFalse(
            user.is_active,
            "El usuario debe quedar desactivado aunque PanAccess haya fallado/quedado parcial",
        )

    @patch("wind.services.subscriber_closure.deprovision_subscriber_in_panaccess")
    def test_deactivates_user_on_full_success(self, mock_deprovision):
        code = "40219990002"
        email = "cliente.cerrado2@example.com"
        user = self._make_user_and_registry(code, email)

        mock_deprovision.return_value = {
            "success": True,
            "subscriber_deleted": True,
            "steps": [],
            "warnings": [],
            "errors": [],
        }

        result = close_subscriber_account(code, reason="prueba")

        self.assertTrue(result["success"], result)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
