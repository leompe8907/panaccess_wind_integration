"""
Endpoint de sincronización global.

Objetivo: con una sola llamada, dejar consistentes las tablas locales con PanAccess:
- ListOfSubscriber (crear/actualizar/borrar sobrantes)
- ListOfProducts (crear/actualizar/borrar sobrantes)
- ListOfSmartcards (crear/actualizar/borrar sobrantes)
- SubscriberLoginInfo (crear/actualizar y borrar sobrantes respecto a suscriptores locales)
"""

import logging

from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework import status

from appConfig import CeleryConfig, FeatureConfig
from wind.throttles import SyncAdminThrottle
from wind.models import ListOfProducts, SubscriberLoginInfo
from wind.functions.getSubscriber import compare_and_update_all_subscribers
from wind.functions.getProducts import sync_products, compare_and_update_all_products, CallListOfProducts
from wind.functions.getSmartcard import compare_and_update_all_smartcards
from wind.functions.getSubscriberLoginInfo import sync_subscribers_login_info

logger = logging.getLogger(__name__)


def _parse_limit(request, default=100) -> int:
    if request.method == "GET":
        raw = request.query_params.get("limit", default)
    else:
        raw = request.data.get("limit", default)
    try:
        limit = int(raw)
    except Exception:
        limit = default
    return min(max(limit, 1), 1000)


def _delete_extra_products(limit: int) -> dict:
    """Elimina productos locales que ya no existan en PanAccess."""
    remote_ids = set()
    offset = 0
    remote_count = None

    while True:
        result = CallListOfProducts(session_id=None, offset=offset, limit=limit)
        entries = result.get("productEntries", []) or []
        if remote_count is None:
            remote_count = int(result.get("count") or 0)

        if not entries:
            break

        for e in entries:
            pid = e.get("productId")
            if pid is not None:
                remote_ids.add(pid)

        offset += limit
        if remote_count and len(remote_ids) >= remote_count:
            break

    local_ids = set(ListOfProducts.objects.values_list("productId", flat=True))
    extra = local_ids - remote_ids
    deleted = 0
    if extra:
        deleted = ListOfProducts.objects.filter(productId__in=extra).delete()[0]

    return {"remote": len(remote_ids), "local": len(local_ids), "extra_deleted": deleted}


def _cleanup_login_info() -> dict:
    """Elimina SubscriberLoginInfo sin suscriptor local (post sync de login)."""
    from wind.functions.getSubscriberLoginInfo import cleanup_login_info_orphans

    deleted_orphans = cleanup_login_info_orphans()
    total = SubscriberLoginInfo.objects.count()
    return {"remaining": total, "deleted_orphans": deleted_orphans}


def run_full_sync(limit: int = 100) -> dict:
    """
    Ejecuta la sincronización global (uso en Celery o admin con HTTP habilitado).

    Returns:
        dict con resultados por dominio (suscriptores, productos, smartcards, login info).
    """
    result_subscribers = compare_and_update_all_subscribers(session_id=None, limit=limit)

    result_products_new = sync_products(session_id=None, limit=limit)
    compare_and_update_all_products(session_id=None, limit=limit)
    result_products_delete = _delete_extra_products(limit=limit)

    result_smartcards = compare_and_update_all_smartcards(
        session_id=None, limit=limit, force_full=True
    )

    result_login_info = sync_subscribers_login_info(session_id=None, limit=None)
    result_login_cleanup = _cleanup_login_info()

    return {
        "success": True,
        "message": "Sincronización global completada",
        "limit_used": limit,
        "subscribers": result_subscribers,
        "products": {
            "sync_result": result_products_new,
            "delete_extras": result_products_delete,
        },
        "smartcards": result_smartcards,
        "subscriber_login_info": {
            "sync_result": result_login_info,
            "cleanup": result_login_cleanup,
        },
    }


def _full_sync_http_enabled() -> bool:
    return getattr(FeatureConfig, 'FULL_SYNC_HTTP_ENABLED', False)


@api_view(["GET", "POST"])
@permission_classes([IsAdminUser])
@throttle_classes([SyncAdminThrottle])
def full_sync_view(request):
    """
    Encola el full-sync correctivo (staff + FULL_SYNC_HTTP_ENABLED=true).

    Por defecto usar Celery Beat nocturno (`full_sync_task`).
    """
    if not _full_sync_http_enabled():
        return Response(
            {
                "success": False,
                "message": (
                    "Full-sync HTTP deshabilitado. Use Celery Beat (tarea full_sync_task) "
                    "o defina FULL_SYNC_HTTP_ENABLED=true solo en entornos controlados."
                ),
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == "GET":
        return Response(
            {
                "success": True,
                "message": "Use POST para encolar la sincronización global.",
                "beat_hour": CeleryConfig.FULL_SYNC_HOUR,
                "beat_minute": CeleryConfig.FULL_SYNC_MINUTE,
            },
            status=status.HTTP_200_OK,
        )

    limit = _parse_limit(request, default=100)
    try:
        from wind.tasks import full_sync_task

        async_result = full_sync_task.delay(limit=limit)
        return Response(
            {
                "success": True,
                "message": "Full-sync encolado",
                "task_id": async_result.id,
                "limit": limit,
                "status_url": f"/api/v1/tasks/{async_result.id}/",
            },
            status=status.HTTP_202_ACCEPTED,
        )
    except Exception as e:
        logger.exception("Error encolando full-sync")
        return Response(
            {"success": False, "error_type": "Exception", "message": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
