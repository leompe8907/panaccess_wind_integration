"""
Vista para sincronizar suscriptores desde PanAccess.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from wind.exceptions import PanAccessException
from wind.functions.getSubscriber import (
    DataBaseEmpty,
    LastSubscriber,
    compare_and_update_all_subscribers,
    sync_subscribers,
)
from wind.services.sync_http import (
    celery_enqueue_response,
    parse_sync_limit,
    sync_get_info_response,
    sync_http_async_enabled,
)
from wind.throttles import SyncAdminThrottle

logger = logging.getLogger(__name__)


@api_view(["GET", "POST"])
@permission_classes([IsAdminUser])
@throttle_classes([SyncAdminThrottle])
def sync_subscribers_view(request):
    if request.method == "GET":
        return sync_get_info_response(
            endpoint="/wind/sync-subscribers/",
            task_name="sync_subscribers_task",
            async_default=True,
        )

    limit = parse_sync_limit(request)
    if sync_http_async_enabled():
        from wind.tasks import sync_subscribers_task

        return celery_enqueue_response(
            sync_subscribers_task.delay(limit=limit),
            limit=limit,
            label="sync-subscribers",
        )

    try:
        result = sync_subscribers(session_id=None, limit=limit)
        last_subscriber = LastSubscriber()
        return Response(
            {
                "success": True,
                "message": "Sincronización completada (síncrona; SYNC_HTTP_ASYNC=false)",
                "limit_used": limit,
                "last_subscriber_code": last_subscriber.code if last_subscriber else None,
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


@api_view(["GET", "POST"])
@permission_classes([IsAdminUser])
@throttle_classes([SyncAdminThrottle])
def compare_and_update_subscribers_view(request):
    if request.method == "GET":
        return sync_get_info_response(
            endpoint="/wind/compare-and-update-subscribers/",
            task_name="compare_and_update_subscribers_task",
            async_default=True,
        )

    limit = parse_sync_limit(request)
    if sync_http_async_enabled():
        from wind.tasks import compare_and_update_subscribers_task

        return celery_enqueue_response(
            compare_and_update_subscribers_task.delay(limit=limit),
            limit=limit,
            label="compare-and-update-subscribers",
        )

    try:
        result = compare_and_update_all_subscribers(session_id=None, limit=limit)
        return Response(
            {
                "success": True,
                "message": "Comparación completada (síncrona)",
                "limit_used": limit,
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
