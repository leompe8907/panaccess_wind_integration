"""
Vista para sincronizar productos desde PanAccess.
"""
import logging
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework import status

from wind.throttles import SyncAdminThrottle

from wind.functions.getProducts import (
    sync_products,
    CallListOfProducts,
    DataBaseEmpty,
    LastProduct
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
def sync_products_view(request):
    if request.method == "GET":
        return sync_get_info_response(
            endpoint="/wind/sync-products/",
            task_name="sync_products_task",
            async_default=True,
        )

    limit = parse_sync_limit(request)
    if sync_http_async_enabled():
        from wind.tasks import sync_products_task

        return celery_enqueue_response(
            sync_products_task.delay(limit=limit),
            limit=limit,
            label="sync-products",
        )

    try:
        result = sync_products(session_id=None, limit=limit)
        last_product = LastProduct()
        return Response(
            {
                "success": True,
                "message": "Sincronización completada (síncrona)",
                "limit_used": limit,
                "last_product_id": last_product.productId if last_product else None,
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
def test_call_list_products(request):
    try:
        if request.method == 'GET':
            offset = int(request.query_params.get('offset', 0))
            limit = int(request.query_params.get('limit', 100))
            orderBy = request.query_params.get('orderBy', 'productId')
            orderDir = request.query_params.get('orderDir', 'ASC')
        else:
            offset = int(request.data.get('offset', 0))
            limit = int(request.data.get('limit', 100))
            orderBy = request.data.get('orderBy', 'productId')
            orderDir = request.data.get('orderDir', 'ASC')
        
        if limit > 1000:
            limit = 1000
        
        if orderDir not in ['ASC', 'DESC']:
            orderDir = 'ASC'
        
        result = CallListOfProducts(
            session_id=None,
            offset=offset,
            limit=limit,
            orderBy=orderBy,
            orderDir=orderDir
        )
        
        count = result.get('count', 0)
        product_entries = result.get('productEntries', [])
        
        return Response({
            'success': True,
            'message': 'Llamada a getListOfProducts exitosa',
            'parameters': {
                'offset': offset,
                'limit': limit,
                'orderBy': orderBy,
                'orderDir': orderDir
            },
            'result': {
                'count': count,
                'product_entries_count': len(product_entries),
                'product_entries': product_entries[:10] if len(product_entries) > 10 else product_entries,
                'has_more': len(product_entries) > 10
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
def products_stats_view(request):
    try:
        from wind.models import ListOfProducts
        
        total_products = ListOfProducts.objects.count()
        last_product = LastProduct()
        database_empty = DataBaseEmpty()
        
        stats = {
            'success': True,
            'total_products': total_products,
            'database_empty': database_empty,
            'last_product_id': last_product.productId if last_product else None,
            'last_product_name': last_product.name if last_product else None,
        }
        
        if not database_empty:
            deleted_count = ListOfProducts.objects.filter(deleted=True).count()
            active_count = ListOfProducts.objects.filter(deleted=False).count()
            with_packages = ListOfProducts.objects.exclude(packages__isnull=True).exclude(packages=[]).count()
            
            stats.update({
                'deleted_products': deleted_count,
                'active_products': active_count,
                'products_with_packages': with_packages,
            })
        
        return Response(stats, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error estadísticas: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'error_type': 'Exception',
            'message': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
