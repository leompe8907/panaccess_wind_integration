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

from wind.services.password_reset import reset_password_in_panaccess, sync_password_locally
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
        reset_password_in_panaccess(code, new_pass)
        email = getattr(request.user, "email", "") or ""
        sync_password_locally(code, email, new_pass)
        return Response(
            {
                "success": True,
                "message": "Reset de contraseña ejecutado",
            },
            status=status.HTTP_200_OK,
        )

    except PanAccessException as e:
        # 502, no 500: el fallo es de la dependencia externa (PanAccess),
        # no de este servicio -- mismo criterio que profile/views.py.
        return Response(
            {"success": False, "error_type": "PanAccessException", "message": str(e)},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    except Exception:
        # No devolver str(e) al cliente -- puede filtrar detalles internos
        # (nombres de tabla, rutas, fragmentos de configuración). El detalle
        # real queda en el log del servidor (ver auditoría).
        logger.exception("Error inesperado en change_password_view para code=%s", code)
        return Response(
            {
                "success": False,
                "error_type": "Exception",
                "message": "Ocurrió un error inesperado al cambiar la contraseña. Intenta de nuevo.",
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
