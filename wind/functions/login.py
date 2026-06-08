"""
Diagnóstico de sesión PanAccess del singleton (cuenta de servicio).

No exponer en /wind/login/ (portal de usuario). Ruta: /wind/ops/panaccess-session/
"""
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework import status

from appConfig import FeatureConfig
from wind.throttles import SyncAdminThrottle
from wind.services import get_panaccess
from wind.exceptions import (
    PanAccessAuthenticationError,
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessAPIError,
    PanAccessException,
)


def _panaccess_ops_http_enabled() -> bool:
    return getattr(FeatureConfig, 'PANACCESS_OPS_HTTP_ENABLED', False)


@api_view(["GET"])
@permission_classes([IsAdminUser])
@throttle_classes([SyncAdminThrottle])
def panaccess_session_status_view(request):
    """
    Estado de la sesión PanAccess del singleton (solo staff + PANACCESS_OPS_HTTP_ENABLED).

    No devuelve session_id (evita filtrar credencial de sistema).
    """
    if not _panaccess_ops_http_enabled():
        return Response(
            {
                "success": False,
                "message": (
                    "Endpoint de operaciones deshabilitado. "
                    "Use PANACCESS_OPS_HTTP_ENABLED=true solo en entornos controlados "
                    "o valide la sesión vía GET /wind/singleton/ (también restringido)."
                ),
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        panaccess = get_panaccess()
        panaccess.ensure_session()
        authenticated = panaccess.client.is_authenticated()

        return Response(
            {
                "success": True,
                "message": "Sesión PanAccess del servicio activa" if authenticated else "Sin sesión",
                "is_authenticated": authenticated,
            },
            status=status.HTTP_200_OK,
        )

    except PanAccessAuthenticationError as e:
        return Response(
            {"success": False, "error_type": "PanAccessAuthenticationError", "message": str(e)},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    except PanAccessConnectionError as e:
        return Response(
            {"success": False, "error_type": "PanAccessConnectionError", "message": str(e)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    except PanAccessTimeoutError as e:
        return Response(
            {"success": False, "error_type": "PanAccessTimeoutError", "message": str(e)},
            status=status.HTTP_504_GATEWAY_TIMEOUT,
        )

    except PanAccessAPIError as e:
        return Response(
            {
                "success": False,
                "error_type": "PanAccessAPIError",
                "message": str(e),
                "status_code": e.status_code,
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )

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
