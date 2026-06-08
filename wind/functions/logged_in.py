"""
Vista para validar si un sessionId está vigente.

Endpoint que prueba la función logged_in() y el método check_session() del cliente.
"""
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAdminUser
from wind.throttles import SyncAdminThrottle
from rest_framework.response import Response
from rest_framework import status

from appConfig import FeatureConfig
from wind.utils.panaccess_auth import logged_in
from wind.services import PanAccessClient
from wind.exceptions import (
    PanAccessAuthenticationError,
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessAPIError,
    PanAccessException
)


def _panaccess_ops_http_enabled() -> bool:
    return getattr(FeatureConfig, 'PANACCESS_OPS_HTTP_ENABLED', False)


@api_view(['GET'])
@permission_classes([IsAdminUser])
@throttle_classes([SyncAdminThrottle])
def logged_in_view(request):
    """
    Vista para validar si un sessionId está vigente.
    
    Prueba la función logged_in() y el método check_session() del cliente.
    """
    if not _panaccess_ops_http_enabled():
        return Response(
            {
                "success": False,
                "message": "Endpoint de operaciones deshabilitado (PANACCESS_OPS_HTTP_ENABLED).",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    client = PanAccessClient()

    try:
        session_id = client.authenticate()
        is_valid_direct = logged_in(session_id)
        is_valid_client = client.check_session()

        return Response(
            {
                "success": True,
                "message": "Verificación de sesión exitosa",
                "session_active": bool(session_id),
                "is_valid_direct": is_valid_direct,
                "is_valid_client": is_valid_client,
                "both_match": is_valid_direct == is_valid_client,
            },
            status=status.HTTP_200_OK,
        )
        
    except PanAccessAuthenticationError as e:
        return Response({
            'success': False,
            'error_type': 'PanAccessAuthenticationError',
            'message': str(e)
        }, status=status.HTTP_401_UNAUTHORIZED)
        
    except PanAccessConnectionError as e:
        return Response({
            'success': False,
            'error_type': 'PanAccessConnectionError',
            'message': str(e)
        }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        
    except PanAccessTimeoutError as e:
        return Response({
            'success': False,
            'error_type': 'PanAccessTimeoutError',
            'message': str(e)
        }, status=status.HTTP_504_GATEWAY_TIMEOUT)
        
    except PanAccessAPIError as e:
        return Response({
            'success': False,
            'error_type': 'PanAccessAPIError',
            'message': str(e),
            'status_code': e.status_code
        }, status=status.HTTP_502_BAD_GATEWAY)
        
    except PanAccessException as e:
        return Response({
            'success': False,
            'error_type': 'PanAccessException',
            'message': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
    except Exception as e:
        return Response({
            'success': False,
            'error_type': 'Exception',
            'message': f'Error inesperado: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
