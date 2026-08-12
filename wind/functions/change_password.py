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
from wind.exceptions import (
    PanAccessException,
    PanAccessAPIError,
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessAuthenticationError,
    PanAccessSessionError,
    PanAccessRateLimitError,
)
from wind.permissions import IsOwnerSubscriber
from wind.throttles import ProfileThrottle
from wind.utils.password_policy import PASSWORD_POLICY_CODE, validate_password_policy

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

    # Validación local de política de contraseña -- ver
    # wind/utils/password_policy.py y auditoría
    # "BACKEND_CHANGE_PASSWORD_VALIDATION_ISSUE". Evita el round-trip a
    # PanAccess para el caso común, y sobre todo evita que ese rechazo
    # salga como 502 (antes indistinguible de una falla real de
    # conectividad con PanAccess).
    policy_error = validate_password_policy(new_pass)
    if policy_error:
        return Response(
            {
                "success": False,
                "error_type": "ValidationError",
                "code": PASSWORD_POLICY_CODE,
                "message": policy_error,
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

    except PanAccessConnectionError as e:
        return Response(
            {"success": False, "error_type": "PanAccessConnectionError", "code": "panaccess_unavailable", "message": str(e)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    except PanAccessTimeoutError as e:
        return Response(
            {"success": False, "error_type": "PanAccessTimeoutError", "code": "panaccess_timeout", "message": str(e)},
            status=status.HTTP_504_GATEWAY_TIMEOUT,
        )

    except (PanAccessAuthenticationError, PanAccessSessionError) as e:
        return Response(
            {"success": False, "error_type": type(e).__name__, "code": "panaccess_integration_error", "message": str(e)},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    except PanAccessRateLimitError as e:
        return Response(
            {"success": False, "error_type": "PanAccessRateLimitError", "code": "rate_limited", "message": str(e)},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    except PanAccessAPIError as e:
        # PanAccess respondió y rechazó el valor -- error de input del
        # cliente (400), no de servidor. Ver mismo criterio en
        # wind/api/profile/views.py::profile_password_view.
        return Response(
            {
                "success": False,
                "error_type": "PanAccessAPIError",
                "code": "password_rejected_by_panaccess",
                "panaccess_error_code": getattr(e, "error_code", None),
                "message": str(e),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    except PanAccessException as e:
        # Base no prevista específicamente -- se mantiene 502 como antes.
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
