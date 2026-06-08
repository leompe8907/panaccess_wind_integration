"""
Vista para sincronizar smartcards desde PanAccess.
"""
import logging
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework import status

from wind.throttles import SyncAdminThrottle

from wind.functions.getSmartcard import (
    sync_smartcards,
    CallListSmartcards,
    DataBaseEmpty,
    LastSmartcard
)
from wind.exceptions import PanAccessException
from wind.services.sync_http import (
    celery_enqueue_response,
    parse_sync_limit,
    sync_get_info_response,
    sync_http_async_enabled,
)

logger = logging.getLogger(__name__)


@api_view(["GET", "POST"])
@permission_classes([IsAdminUser])
@throttle_classes([SyncAdminThrottle])
def sync_smartcards_view(request):
    if request.method == "GET":
        return sync_get_info_response(
            endpoint="/wind/sync-smartcards/",
            task_name="sync_smartcards_task",
            async_default=True,
        )

    limit = parse_sync_limit(request)
    if sync_http_async_enabled():
        from wind.tasks import sync_smartcards_task

        return celery_enqueue_response(
            sync_smartcards_task.delay(limit=limit),
            limit=limit,
            label="sync-smartcards",
        )

    try:
        result = sync_smartcards(session_id=None, limit=limit)
        last_smartcard = LastSmartcard()
        return Response(
            {
                "success": True,
                "message": "Sincronización completada (síncrona)",
                "limit_used": limit,
                "last_smartcard_sn": last_smartcard.sn if last_smartcard else None,
                "database_empty": DataBaseEmpty(),
                "result": result,
            },
            status=status.HTTP_200_OK,
        )
    except PanAccessException as e:
        logger.error("Error PanAccess: %s", e)
        return Response(
            {"success": False, "error_type": type(e).__name__, "message": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except ValueError as e:
        return Response(
            {"success": False, "error_type": "ValueError", "message": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        return Response(
            {"success": False, "error_type": "Exception", "message": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
@throttle_classes([SyncAdminThrottle])
def test_call_list_smartcards(request):
    try:
        if request.method == 'GET':
            offset = int(request.query_params.get('offset', 0))
            limit = int(request.query_params.get('limit', 100))
        else:
            offset = int(request.data.get('offset', 0))
            limit = int(request.data.get('limit', 100))
        
        if limit > 1000:
            limit = 1000
        
        result = CallListSmartcards(
            session_id=None,
            offset=offset,
            limit=limit
        )
        
        count = result.get('count', 0)
        rows = result.get('rows', [])
        
        return Response({
            'success': True,
            'message': 'Llamada a getListOfSmartcards exitosa',
            'parameters': {
                'offset': offset,
                'limit': limit
            },
            'result': {
                'count': count,
                'rows_count': len(rows),
                'rows': rows[:10] if len(rows) > 10 else rows,
                'has_more': len(rows) > 10
            }
        }, status=status.HTTP_200_OK)
        
    except PanAccessException as e:
        logger.error(f"Error PanAccess: {str(e)}")
        return Response({
            'success': False,
            'error_type': type(e).__name__,
            'message': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
    except ValueError as e:
        logger.error(f"Error parámetros: {str(e)}")
        return Response({
            'success': False,
            'error_type': 'ValueError',
            'message': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)
        
    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'error_type': 'Exception',
            'message': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAdminUser])
@throttle_classes([SyncAdminThrottle])
def smartcards_stats_view(request):
    try:
        from wind.models import ListOfSmartcards
        
        total_smartcards = ListOfSmartcards.objects.count()
        last_smartcard = LastSmartcard()
        database_empty = DataBaseEmpty()
        
        stats = {
            'success': True,
            'total_smartcards': total_smartcards,
            'database_empty': database_empty,
            'last_smartcard_sn': last_smartcard.sn if last_smartcard else None,
            'last_smartcard_name': f"{last_smartcard.firstName or ''} {last_smartcard.lastName or ''}".strip() if last_smartcard else None,
        }
        
        if not database_empty:
            blacklisted_count = ListOfSmartcards.objects.filter(blacklisted=True).count()
            disabled_count = ListOfSmartcards.objects.filter(disabled=True).count()
            active_count = ListOfSmartcards.objects.filter(blacklisted=False, disabled=False).count()
            
            stats.update({
                'blacklisted_smartcards': blacklisted_count,
                'disabled_smartcards': disabled_count,
                'active_smartcards': active_count,
            })
        
        return Response(stats, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error estadísticas: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'error_type': 'Exception',
            'message': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
