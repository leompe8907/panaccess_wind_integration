"""
Vista para reset de contraseña de suscriptor en PanAccess.

Endpoint que llama a la función remota `resetSubscriberPassword` de PanAccess.
Usa la misma lógica que el resto de funciones: toma el `sessionId` desde
el singleton (`get_panaccess()`), que mantiene una sesión activa al levantar
el proyecto (y la refresca si es necesario).
"""

import logging
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from wind.services import get_panaccess
from wind.exceptions import PanAccessException
from wind.permissions import IsOwnerSubscriber
from wind.throttles import ProfileThrottle

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOwnerSubscriber])
@throttle_classes([ProfileThrottle])
def change_password_view(request):
    """
    Cambia la contraseña en PanAccess (propio suscriptor).

    Preferir: POST /api/v1/profile/password/

    Body JSON:
      - code: string (debe coincidir con el suscriptor del usuario JWT)
      - newPass: string
    """
    code = request.data.get("code")
    new_pass = request.data.get("newPass")

    if not code or not new_pass:
        return Response(
            {
                "success": False,
                "error_type": "ValidationError",
                "message": "Faltan campos requeridos: code, newPass",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        panaccess = get_panaccess()
        parameters = {"code": code, "newPass": new_pass, "hash": False}
        response = panaccess.call("resetSubscriberPassword", parameters)

        if response.get("success"):
            return Response(
                {
                    "success": True,
                    "message": "Reset de contraseña ejecutado",
                    "result": response.get("answer", response),
                },
                status=status.HTTP_200_OK,
            )

        error_message = response.get("errorMessage", "Error desconocido al resetear contraseña")
        logger.error(f"Error PanAccess: {error_message}")
        raise PanAccessException(error_message)

    except PanAccessException as e:
        return Response(
            {"success": False, "error_type": "PanAccessException", "message": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    except Exception as e:
        return Response(
            {"success": False, "error_type": "Exception", "message": f"Error inesperado: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
