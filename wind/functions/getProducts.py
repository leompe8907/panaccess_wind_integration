"""
Funciones para obtener y sincronizar productos desde PanAccess.
"""
import logging
from django.db import transaction
from appConfig import PanaccessConfig
from wind.models import ListOfProducts
from wind.services import get_panaccess
from wind.exceptions import PanAccessException

logger = logging.getLogger(__name__)


def DataBaseEmpty():
    """
    Verifica si la tabla ListOfProducts está vacía.
    """
    return not ListOfProducts.objects.exists()


def LastProduct():
    """
    Obtiene el último producto registrado (por productId).
    """
    return ListOfProducts.objects.order_by('-productId').first()


def store_all_products_in_chunks(data_batch, chunk_size=None):
    """
    Almacena productos en la base de datos en bloques para mejorar el rendimiento.
    """
    chunk_size = chunk_size or PanaccessConfig.DB_WRITE_CHUNK_SIZE
    total = len(data_batch)
    if total == 0:
        return
    logger.info(f"Almacenando {total} productos")
    for i in range(0, total, chunk_size):
        chunk = data_batch[i:i + chunk_size]
        try:
            registros = [ListOfProducts(**item) for item in chunk]
            ListOfProducts.objects.bulk_create(registros, ignore_conflicts=True)
        except Exception as e:
            logger.error(f"Error insertando chunk {i//chunk_size + 1}: {str(e)}")
    logger.info(f"Almacenados {total} productos")


def fetch_all_products(session_id=None, limit=100):
    logger.info("Descarga completa de productos")
    offset = 0
    all_data = []
    
    while True:
        result = CallListOfProducts(session_id, offset, limit)
        product_entries = result.get("productEntries", [])
        
        if not product_entries:
            break
        
        for entry in product_entries:
            all_data.append({
                "productId": entry.get("productId"),
                "name": entry.get("name"),
                "ordered": entry.get("ordered", 0),
                "activeOrders": entry.get("activeOrders", 0),
                "flexiblyOrdered": entry.get("flexiblyOrdered", 0),
                "activeFlexibleOrders": entry.get("activeFlexibleOrders", 0),
                "deleted": entry.get("deleted", False),
                "description": entry.get("description"),
                "minRunTime": entry.get("minRunTime", 0),
                "minRunTimeType": entry.get("minRunTimeType"),
                "allowFlexibleRuntime": entry.get("allowFlexibleRuntime", False),
                "hasOptionalPackages": entry.get("hasOptionalPackages", False),
                "packages": entry.get("packages", []),
                "optionalPackages": entry.get("optionalPackages", []),
                "catchupGroups": entry.get("catchupGroups", []),
                "streams": entry.get("streams", []),
                "vodLibraries": entry.get("vodLibraries", []),
            })
        
        offset += limit
        total_count = result.get("count", 0)
        if len(all_data) >= total_count:
            break
    
    logger.info(f"Descargados {len(all_data)} productos")
    return store_all_products_in_chunks(all_data)


def download_products_since_last(session_id=None, limit=100):
    last = LastProduct()
    if not last:
        return []
    
    highest_product_id = last.productId
    logger.info(f"Descarga incremental desde ID: {highest_product_id}")
    offset = 0
    new_data = []
    found = False
    
    while True:
        result = CallListOfProducts(session_id, offset, limit)
        product_entries = result.get("productEntries", [])
        
        if not product_entries:
            break
        
        for entry in product_entries:
            product_id = entry.get("productId")
            if product_id == highest_product_id:
                found = True
                break
            
            new_data.append({
                "productId": product_id,
                "name": entry.get("name"),
                "ordered": entry.get("ordered", 0),
                "activeOrders": entry.get("activeOrders", 0),
                "flexiblyOrdered": entry.get("flexiblyOrdered", 0),
                "activeFlexibleOrders": entry.get("activeFlexibleOrders", 0),
                "deleted": entry.get("deleted", False),
                "description": entry.get("description"),
                "minRunTime": entry.get("minRunTime", 0),
                "minRunTimeType": entry.get("minRunTimeType"),
                "allowFlexibleRuntime": entry.get("allowFlexibleRuntime", False),
                "hasOptionalPackages": entry.get("hasOptionalPackages", False),
                "packages": entry.get("packages", []),
                "optionalPackages": entry.get("optionalPackages", []),
                "catchupGroups": entry.get("catchupGroups", []),
                "streams": entry.get("streams", []),
                "vodLibraries": entry.get("vodLibraries", []),
            })
        
        if found:
            break
        offset += limit
    
    logger.info(f"Nuevos productos descargados: {len(new_data)}")
    return store_all_products_in_chunks(new_data)


def compare_and_update_all_products(session_id=None, limit=100):
    logger.info("Actualizando productos existentes")
    local_data = {
        obj.productId: obj for obj in ListOfProducts.objects.all()
    }
    offset = 0
    total_updated = 0
    
    while True:
        response = CallListOfProducts(session_id, offset, limit)
        product_entries = response.get("productEntries", [])
        
        if not product_entries:
            break
        
        for entry in product_entries:
            product_id = entry.get("productId")
            if not product_id or product_id not in local_data:
                continue
            
            remote = {
                "name": entry.get("name"),
                "ordered": entry.get("ordered", 0),
                "activeOrders": entry.get("activeOrders", 0),
                "flexiblyOrdered": entry.get("flexiblyOrdered", 0),
                "activeFlexibleOrders": entry.get("activeFlexibleOrders", 0),
                "deleted": entry.get("deleted", False),
                "description": entry.get("description"),
                "minRunTime": entry.get("minRunTime", 0),
                "minRunTimeType": entry.get("minRunTimeType"),
                "allowFlexibleRuntime": entry.get("allowFlexibleRuntime", False),
                "hasOptionalPackages": entry.get("hasOptionalPackages", False),
                "packages": entry.get("packages", []),
                "optionalPackages": entry.get("optionalPackages", []),
                "catchupGroups": entry.get("catchupGroups", []),
                "streams": entry.get("streams", []),
                "vodLibraries": entry.get("vodLibraries", []),
            }
            
            local_obj = local_data[product_id]
            changed_fields = []
            
            for key, val in remote.items():
                if hasattr(local_obj, key):
                    local_val = getattr(local_obj, key)
                    if isinstance(local_val, list) and isinstance(val, list):
                        if local_val != val:
                            setattr(local_obj, key, val)
                            changed_fields.append(key)
                    elif str(local_val) != str(val):
                        setattr(local_obj, key, val)
                        changed_fields.append(key)
            
            if changed_fields:
                try:
                    local_obj.save(update_fields=changed_fields)
                    total_updated += 1
                except Exception as e:
                    logger.error(f"Error actualizando producto {product_id}: {str(e)}")
        
        offset += limit
    
    logger.info(f"Actualizados {total_updated} productos")


def sync_products(session_id=None, limit=100):
    logger.info("Sincronizando productos")
    try:
        if DataBaseEmpty():
            return fetch_all_products(session_id, limit)
        else:
            new_result = download_products_since_last(session_id, limit)
            compare_and_update_all_products(session_id, limit)
            return new_result
    except PanAccessException as e:
        logger.error(f"Error PanAccess: {str(e)}")
        raise
    except (ConnectionError, ValueError) as e:
        logger.error(f"Error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error inesperado: {str(e)}", exc_info=True)
        raise


def CallListOfProducts(session_id=None, offset=0, limit=100):
    try:
        panaccess = get_panaccess()
        if limit > 1000:
            limit = 1000
        
        parameters = {
            'offset': offset,
            'limit': limit,
            'orderBy': 'productId',
            'orderDir': 'DESC'
        }
        response = panaccess.call('getListOfProducts', parameters)

        if response.get('success'):
            return response.get('answer', {})
        else:
            error_message = response.get('errorMessage', 'Error desconocido al obtener productos')
            logger.error(f"Error PanAccess: {error_message}")
            raise PanAccessException(error_message)
    except PanAccessException:
        raise
    except Exception as e:
        logger.error(f"Error llamada API: {str(e)}", exc_info=True)
        raise
