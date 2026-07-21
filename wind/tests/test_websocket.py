import json
from django.test import TransactionTestCase
from channels.testing import WebsocketCommunicator
from channels.routing import ProtocolTypeRouter, URLRouter
from django.conf import settings
from unittest.mock import patch, MagicMock

from wind.routing import websocket_urlpatterns
from wind.models import UDIDAuthRequest


# Definir la aplicación para las pruebas
application = ProtocolTypeRouter({
    "websocket": URLRouter(websocket_urlpatterns),
})


class WebSocketPairingTestCase(TransactionTestCase):
    async def test_websocket_connect_and_auth_flow(self):
        from asgiref.sync import sync_to_async
        
        # Conectar al WebSocket
        communicator = WebsocketCommunicator(application, "/ws/auth/")
        
        # Mockear las funciones de rate limit y fingerprinting en websocket_utils.
        # Antes también se mockeaban check_websocket_rate_limit/
        # increment_websocket_connection/decrement_websocket_connection --
        # ese sistema de conteo (basado en cache de Django) se consolidó
        # dentro de check_websocket_limits/decrement_websocket_limits (ver
        # auditoría), consumers.py ya no los importa.
        with patch('wind.consumers.check_websocket_limits', return_value=(True, "", 0)), \
             patch('wind.consumers.decrement_websocket_limits'):

            connected, subprotocol = await communicator.connect()
            self.assertTrue(connected)

            # Crear un registro de UDID de prueba en estado pendiente
            udid_request = await sync_to_async(UDIDAuthRequest.objects.create)(
                udid="testudid",
                status="pending",
                method="manual"
            )

            # Enviar mensaje de autenticación con el UDID. temp_token ahora
            # es obligatorio (ver auditoría: el udid de 8 caracteres ya no
            # alcanza por sí solo como credencial del pareo).
            await communicator.send_json_to({
                "type": "auth_with_udid",
                "udid": "testudid",
                "temp_token": udid_request.temp_token,
                "app_type": "androidtv",
                "app_version": "1.0"
            })
            
            # Esperar respuesta de estado pendiente (porque no se ha validado aún)
            response = await communicator.receive_json_from()
            self.assertEqual(response["type"], "pending")
            self.assertEqual(response["status"], "pending")
            
            # Desconectar el WebSocket
            await communicator.disconnect()
