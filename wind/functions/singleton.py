"""
Diagnóstico del singleton PanAccess (solo staff, sin exponer session_id completo).
"""
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework import status

from appConfig import FeatureConfig
from wind.throttles import SyncAdminThrottle
from wind.services import get_panaccess
from wind.exceptions import PanAccessException


def _panaccess_ops_http_enabled() -> bool:
    return getattr(FeatureConfig, 'PANACCESS_OPS_HTTP_ENABLED', False)


@api_view(["GET"])
@permission_classes([IsAdminUser])
@throttle_classes([SyncAdminThrottle])
def singleton(request):
    """Prueba cvLoggedIn sin filtrar el sessionId en la respuesta."""
    if not _panaccess_ops_http_enabled():
        return Response(
            {
                "success": False,
                "message": "Endpoint de operaciones deshabilitado (PANACCESS_OPS_HTTP_ENABLED).",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        panaccess = get_panaccess()
        panaccess.ensure_session()
        result = panaccess.call(
            "cvLoggedIn",
            {"sessionId": panaccess.client.session_id},
        )

        return Response(
            {
                "success": True,
                "message": "Singleton operativo",
                "has_session": panaccess.client.is_authenticated(),
                "session_active": bool(panaccess.client.session_id),
                "cv_logged_in": result.get("success") if isinstance(result, dict) else None,
            },
            status=status.HTTP_200_OK,
        )

    except PanAccessException as e:
        return Response(
            {"success": False, "error_type": type(e).__name__, "message": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    except Exception as e:
        return Response(
            {"success": False, "error_type": "Exception", "message": f"Error inesperado: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
