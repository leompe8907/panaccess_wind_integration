"""
Tests de wind.services.panaccess_deprovision con PanAccess mockeado (sin red).

Cubre:
  - Caso feliz: smartcard con producto asociado se limpia (via cleanSmartcards)
    y el suscriptor se borra.
  - Caso real encontrado en la prueba dry-run contra el subscriber 1120743001:
    una orden sin smartcard asociada (sn=None) se resuelve con
    disableOrderOfSubscriber en vez de las llamadas basadas en smartcard.
  - Fallback sin-prefijo -> cv-prefijo: confirmado empiricamente en produccion
    (dos corridas reales) que la cuenta de servicio NO tiene permiso para las
    variantes "cv...", por eso ahora se intenta primero sin prefijo (igual
    que el resto de las llamadas del proyecto) y "cv..." queda solo como
    respaldo automatico en cada paso.
  - Falla parcial: deleteSubscriber devuelve subscriber_has_smartcards porque
    una smartcard no se pudo desvincular (ni con el nombre configurado ni con
    su alterno) -> success=False.
  - dry_run: no dispara ninguna llamada mutable, solo devuelve el plan.

Nota: removeProductFromSmartcards y removeSmartcardFromOrder se probaron
contra PanAccess real y no hacian falta -- cleanSmartcards por si sola limpia
todo, asi que ya no forman parte del flujo (confirmado por el equipo tras una
prueba real de punta a punta).
"""
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase

from wind.services.panaccess_deprovision import deprovision_subscriber_in_panaccess
from appConfig import PanaccessConfig


def _subscriber_response(code, smartcards):
    return {
        "success": True,
        "answer": {
            "code": code,
            "subscriberCode": code,
            "smartcards": smartcards,
        },
    }


def _denied(method):
    return {"success": False, "errorMessage": "Ud. no tiene los permisos para ejecutar esta funcionalidad"}


class DeprovisionHappyPathTestCase(SimpleTestCase):
    """Smartcard con un producto asociado: se limpia con cleanSmartcards y se borra el suscriptor."""

    @patch("wind.functions.getSubscriber.get_panaccess")
    @patch("wind.services.panaccess_deprovision.get_panaccess")
    def test_full_flow_success(self, mock_get_panaccess_deprov, mock_get_panaccess_getsub):
        code = "1120743001"
        sn = "123456789012345"
        mock_client = MagicMock()
        mock_get_panaccess_deprov.return_value = mock_client
        mock_get_panaccess_getsub.return_value = mock_client

        def side_effect(method, params=None, timeout=None):
            params = params or {}
            if method == "getSubscriber":
                return _subscriber_response(code, [sn])
            if method == "getOrdersOfSubscriber":
                return {
                    "success": True,
                    "answer": [
                        {"orderId": 90, "productId": 1, "sn": sn, "disabled": False},
                    ],
                }
            if method == PanaccessConfig.REMOVE_LICENSE_BLOCK_API:
                return {"success": True, "answer": True}
            if method == PanaccessConfig.CLEAN_SMARTCARDS_API:
                self.assertEqual(params.get("smartcards[0]"), sn)
                return {"success": True, "answer": True}
            if method == PanaccessConfig.REMOVE_SMARTCARD_API:
                self.assertEqual(params.get("smartcardId"), sn)
                return {"success": True, "answer": True}
            if method == PanaccessConfig.DELETE_SUBSCRIBER_API:
                self.assertEqual(params.get("code"), code)
                return {"success": True, "answer": True}
            return {"success": False, "errorMessage": f"unexpected call: {method}"}

        mock_client.call.side_effect = side_effect

        result = deprovision_subscriber_in_panaccess(code)

        self.assertTrue(result["success"], result)
        self.assertTrue(result["subscriber_deleted"])
        self.assertEqual(result["smartcards_removed"], 1)
        self.assertEqual(result["errors"], [])

        step_names = [s["step"] for s in result["steps"]]
        self.assertIn("remove_license_block", step_names)
        self.assertIn("clean_smartcards", step_names)
        self.assertIn(f"remove_smartcard_{sn}", step_names)
        self.assertIn("delete_subscriber", step_names)
        # Ya no forman parte del flujo: se probaron y no hacian falta.
        self.assertNotIn("remove_product_1", step_names)
        self.assertNotIn("remove_smartcard_from_order_90", step_names)
        self.assertNotIn("disable_order_90", step_names)  # si tenia smartcard


class DeprovisionOrderWithoutSmartcardTestCase(SimpleTestCase):
    """
    Caso real detectado en la prueba dry-run contra 1120743001: orden activa
    (productId 1) sin smartcard asociada (sn=None, smartcards=[]), y la
    cuenta de servicio sin permiso para "cvGetOrdersOfSubscriber" (se resuelve
    con el fallback a "getOrdersOfSubscriber").
    """

    @patch("wind.functions.getSubscriber.get_panaccess")
    @patch("wind.services.panaccess_deprovision.get_panaccess")
    def test_orderless_order_uses_disable_order(self, mock_get_panaccess_deprov, mock_get_panaccess_getsub):
        code = "1120743001"
        mock_client = MagicMock()
        mock_get_panaccess_deprov.return_value = mock_client
        mock_get_panaccess_getsub.return_value = mock_client

        calls = []

        def side_effect(method, params=None, timeout=None):
            params = params or {}
            calls.append((method, dict(params)))
            if method == "getSubscriber":
                return _subscriber_response(code, [])
            if method == "cvGetOrdersOfSubscriber":
                return _denied(method)
            if method == "getOrdersOfSubscriber":
                return {
                    "success": True,
                    "answer": [
                        {"orderId": 90, "productId": 1, "sn": None, "disabled": False},
                    ],
                }
            if method == PanaccessConfig.DISABLE_ORDER_API:
                self.assertEqual(params.get("orderId"), 90)
                self.assertEqual(params.get("subscriberCode"), code)
                return {"success": True, "answer": True}
            if method == PanaccessConfig.DELETE_SUBSCRIBER_API:
                return {"success": True, "answer": True}
            if method == PanaccessConfig.REMOVE_LICENSE_BLOCK_API:
                return {"success": True, "answer": True}
            return {"success": False, "errorMessage": f"unexpected call: {method}"}

        mock_client.call.side_effect = side_effect

        result = deprovision_subscriber_in_panaccess(code)

        self.assertTrue(result["success"], result)
        self.assertTrue(result["subscriber_deleted"])
        self.assertEqual(result["smartcards_processed"], 0)

        step_names = [s["step"] for s in result["steps"]]
        self.assertIn("disable_order_90", step_names)
        self.assertNotIn("clean_smartcards", step_names)  # no hay smartcards que limpiar

        called_methods = [m for m, _ in calls]
        self.assertIn("getOrdersOfSubscriber", called_methods)
        self.assertNotIn("cvGetOrdersOfSubscriber", called_methods)


class DeprovisionPartialFailureTestCase(SimpleTestCase):
    """
    deleteSubscriber falla por subscriber_has_smartcards (ni el nombre
    configurado ni su alterno "cv" funcionan) -> success=False.
    """

    @patch("wind.functions.getSubscriber.get_panaccess")
    @patch("wind.services.panaccess_deprovision.get_panaccess")
    def test_delete_subscriber_fails_when_smartcard_not_detached(
        self, mock_get_panaccess_deprov, mock_get_panaccess_getsub
    ):
        code = "1120743001"
        sn = "123456789012345"
        mock_client = MagicMock()
        mock_get_panaccess_deprov.return_value = mock_client
        mock_get_panaccess_getsub.return_value = mock_client

        def side_effect(method, params=None, timeout=None):
            params = params or {}
            if method == "getSubscriber":
                return _subscriber_response(code, [sn])
            if method in ("getOrdersOfSubscriber", "cvGetOrdersOfSubscriber"):
                return {"success": True, "answer": []}
            if method in (PanaccessConfig.REMOVE_SMARTCARD_API, "cvRemoveSmartcardFromSubscriber"):
                return {"success": False, "errorMessage": "not_a_smartcard"}
            if method in (PanaccessConfig.DELETE_SUBSCRIBER_API, "cvDeleteSubscriber"):
                return {"success": False, "errorMessage": "subscriber_has_smartcards"}
            return {"success": True, "answer": True}

        mock_client.call.side_effect = side_effect

        result = deprovision_subscriber_in_panaccess(code)

        self.assertFalse(result["success"])
        self.assertFalse(result["subscriber_deleted"])
        self.assertEqual(result["smartcards_removed"], 0)
        self.assertTrue(any("delete_subscriber" in e for e in result["errors"]))
        self.assertTrue(any(f"remove_smartcard_{sn}" in e for e in result["errors"]))


class DeprovisionDryRunTestCase(SimpleTestCase):
    """dry_run=True no debe disparar ninguna llamada mutable."""

    @patch("wind.functions.getSubscriber.get_panaccess")
    @patch("wind.services.panaccess_deprovision.get_panaccess")
    def test_dry_run_only_reads(self, mock_get_panaccess_deprov, mock_get_panaccess_getsub):
        code = "1120743001"
        mock_client = MagicMock()
        mock_get_panaccess_deprov.return_value = mock_client
        mock_get_panaccess_getsub.return_value = mock_client

        read_only_methods = {"getSubscriber", "getOrdersOfSubscriber", "cvGetOrdersOfSubscriber"}

        def side_effect(method, params=None, timeout=None):
            if method == "getSubscriber":
                return _subscriber_response(code, [])
            if method == "getOrdersOfSubscriber":
                return {"success": True, "answer": [{"orderId": 90, "productId": 1, "sn": None, "disabled": False}]}
            self.fail(f"No deberia llamarse '{method}' en dry_run")

        mock_client.call.side_effect = side_effect

        result = deprovision_subscriber_in_panaccess(code, dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["plan"]["subscriber_code"], code)
        self.assertEqual(result["plan"]["product_ids"], ["1"])
        for call_args in mock_client.call.call_args_list:
            method = call_args.args[0] if call_args.args else call_args.kwargs.get("method")
            self.assertIn(method, read_only_methods)
