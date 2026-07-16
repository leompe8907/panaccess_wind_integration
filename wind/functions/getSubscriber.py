import logging
from django.db import transaction
from django.utils import timezone
from appConfig import PanaccessConfig
from wind.models import (
    ListOfSubscriber,
    SubscriberLoginInfo,
    SubscriberEmailRegistry,
    SubscriberDocumentRegistry,
    SubscriberInfo
)
from wind.serializers import ListOfSubscriberSerializer

from wind.services import get_panaccess
from wind.exceptions import PanAccessException

# Importar función para obtener login info
try:
    from wind.functions.getSubscriberLoginInfo import fetch_login_info_for_subscriber
except ImportError:
    fetch_login_info_for_subscriber = None

logger = logging.getLogger(__name__)


def extract_first_email(emails_data):
    """
    Extrae el primer email de una lista o retorna None.
    """
    if not emails_data:
        return None
    
    if isinstance(emails_data, list):
        if len(emails_data) > 0 and emails_data[0]:
            return emails_data[0].lower().strip() if isinstance(emails_data[0], str) else None
        return None
    
    if isinstance(emails_data, str):
        return emails_data.lower().strip()
    
    return None

def extract_first_phone(phones_data):
    """
    Extrae el primer teléfono de una lista o retorna None.
    """
    if not phones_data:
        return None
    
    if isinstance(phones_data, list):
        if len(phones_data) > 0 and phones_data[0]:
            return str(phones_data[0]).strip() if phones_data[0] else None
        return None
    
    if isinstance(phones_data, str):
        return phones_data.strip()
    
    return None


def _get_dateutil_parser():
    try:
        from dateutil import parser as date_parser
        return date_parser
    except ImportError:
        logger.warning("python-dateutil no está instalado, las fechas pueden no parsearse correctamente")
        return None


def _parse_subscriber_datetime(value, parser):
    if not value:
        return None
    try:
        from datetime import datetime, timezone as dt_timezone

        from django.utils import timezone

        if isinstance(value, datetime):
            parsed = value
        elif parser:
            parsed = parser.parse(value)
        else:
            return value

        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed, dt_timezone.utc)
        return parsed
    except Exception as e:
        logger.warning("Error parseando fecha %s: %s", value, e)
        return None


def extended_subscriber_row_to_data(row, parser=None):
    """
    Convierte una fila de getListOfExtendedSubscribers al dict usado por ListOfSubscriber.
    """
    subscriber_code = row.get("subscriberCode")
    if not subscriber_code or not str(subscriber_code).strip():
        return None

    if parser is None:
        parser = _get_dateutil_parser()

    return {
        "id": subscriber_code,
        "code": subscriber_code,
        "lastName": row.get("lastName"),
        "firstName": row.get("firstName"),
        "smartcards": row.get("smartcards"),
        "regionId": row.get("regionId"),
        "countryCode": row.get("countryCode"),
        "caf": row.get("caf"),
        "supervisor": row.get("supervisor"),
        "comment": row.get("comment"),
        "ip": row.get("ip"),
        "emails": extract_first_email(row.get("emails")),
        "phones": extract_first_phone(row.get("phones")),
        "faxes": row.get("faxes"),
        "skypes": row.get("skypes"),
        "mobiles": row.get("mobiles"),
        "custodians": row.get("custodians"),
        "address1": row.get("address1"),
        "address2": row.get("address2"),
        "address3": row.get("address3"),
        "addressCount": row.get("addressCount", 0),
        "newsletterAccepted": row.get("newsletterAccepted", False),
        "tags": row.get("tags"),
        "uniqueLogin": row.get("uniqueLogin"),
        "created": _parse_subscriber_datetime(row.get("created"), parser),
        "firstOrderTime": _parse_subscriber_datetime(row.get("firstOrderTime"), parser),
        "lastExpiryTime": _parse_subscriber_datetime(row.get("lastExpiryTime"), parser),
    }


def _is_closure_tombstone(subscriber) -> bool:
    """Cuentas cerradas / en cierre: no borrar ni reactivar desde sync PanAccess."""
    status = getattr(subscriber, "status", None)
    return status in (
        ListOfSubscriber.STATUS_CLOSED,
        ListOfSubscriber.STATUS_PENDING_CLOSURE,
    )


def _update_subscriber_from_row(local_obj, row, parser=None):
    """Actualiza un suscriptor local si difiere de la fila remota. Retorna True si guardó cambios."""
    if _is_closure_tombstone(local_obj):
        return False

    if parser is None:
        parser = _get_dateutil_parser()

    field_mapping = {
        "lastName": row.get("lastName"),
        "firstName": row.get("firstName"),
        "smartcards": row.get("smartcards"),
        "regionId": row.get("regionId"),
        "countryCode": row.get("countryCode"),
        "caf": row.get("caf"),
        "supervisor": row.get("supervisor"),
        "comment": row.get("comment"),
        "ip": row.get("ip"),
        "emails": extract_first_email(row.get("emails")),
        "phones": extract_first_phone(row.get("phones")),
        "faxes": row.get("faxes"),
        "skypes": row.get("skypes"),
        "mobiles": row.get("mobiles"),
        "custodians": row.get("custodians"),
        "address1": row.get("address1"),
        "address2": row.get("address2"),
        "address3": row.get("address3"),
        "addressCount": row.get("addressCount", 0),
        "newsletterAccepted": row.get("newsletterAccepted", False),
        "tags": row.get("tags"),
        "uniqueLogin": row.get("uniqueLogin"),
        "created": _parse_subscriber_datetime(row.get("created"), parser),
        "firstOrderTime": _parse_subscriber_datetime(row.get("firstOrderTime"), parser),
        "lastExpiryTime": _parse_subscriber_datetime(row.get("lastExpiryTime"), parser),
    }

    changed_fields = []
    for key, val in field_mapping.items():
        if not hasattr(local_obj, key):
            continue
        local_val = getattr(local_obj, key)
        if isinstance(local_val, list) and isinstance(val, list):
            if local_val != val:
                setattr(local_obj, key, val)
                changed_fields.append(key)
        elif isinstance(local_val, dict) and isinstance(val, dict):
            if local_val != val:
                setattr(local_obj, key, val)
                changed_fields.append(key)
        elif str(local_val) != str(val):
            setattr(local_obj, key, val)
            changed_fields.append(key)

    if not changed_fields:
        return False

    try:
        local_obj.save(update_fields=changed_fields)
        return True
    except Exception as e:
        logger.error("Error actualizando suscriptor %s: %s", local_obj.code, e)
        return False


def _delete_local_subscribers_not_in_remote(local_codes, remote_codes):
    """Elimina suscriptores locales y credenciales que no existen en PanAccess.

    Conserva filas con status closed / pending_closure (tombstone de cierre de cuenta).
    """
    missing_remotely = local_codes - remote_codes
    preserved_codes = set(
        ListOfSubscriber.objects.filter(
            code__in=missing_remotely,
            status__in=(
                ListOfSubscriber.STATUS_CLOSED,
                ListOfSubscriber.STATUS_PENDING_CLOSURE,
            ),
        ).values_list("code", flat=True)
    )
    codes_to_delete = missing_remotely - preserved_codes
    total_deleted = 0
    credentials_deleted = {}

    if preserved_codes:
        logger.info(
            "Sync conserva %s suscriptores closed/pending_closure ausentes en PanAccess "
            "(muestra): %s",
            len(preserved_codes),
            list(preserved_codes)[:10],
        )

    if codes_to_delete:
        try:
            credentials_deleted = delete_subscriber_credentials(codes_to_delete)
            total_deleted = ListOfSubscriber.objects.filter(code__in=codes_to_delete).delete()[0]
            logger.info(
                "Eliminados %s suscriptores que ya no existen en PanAccess (muestra): %s",
                total_deleted,
                list(codes_to_delete)[:10],
            )
        except Exception as e:
            logger.error("Error de eliminación: %s", e)

    return {
        "deleted": total_deleted,
        "codes_to_delete_count": len(codes_to_delete),
        "preserved_closed": len(preserved_codes),
        "credentials_deleted": credentials_deleted,
    }


def _cleanup_invalid_local_subscribers():
    """Borra filas locales inválidas (code/id vacíos)."""
    invalid_deleted = 0
    try:
        invalid_deleted += ListOfSubscriber.objects.filter(code__isnull=True).delete()[0]
        invalid_deleted += ListOfSubscriber.objects.filter(code="").delete()[0]
        invalid_deleted += ListOfSubscriber.objects.filter(id="").delete()[0]
        if invalid_deleted:
            logger.info("Eliminados %s suscriptores locales inválidos", invalid_deleted)
    except Exception as e:
        logger.error("Error de limpieza: %s", e)
    return invalid_deleted


def DataBaseEmpty():
    return not ListOfSubscriber.objects.exists()


def delete_subscriber_credentials(subscriber_codes):
    return delete_subscriber_operational_data(
        subscriber_codes,
        preserve_registry=False,
    )


def delete_subscriber_operational_data(subscriber_codes, *, preserve_registry: bool = False):
    """
    Elimina datos operativos locales del suscriptor.

    preserve_registry=True (cierre de cuenta): conserva SubscriberEmailRegistry y
    SubscriberDocumentRegistry como tombstone antifraude.
    """
    if not subscriber_codes:
        return {
            'login_info': 0,
            'email_registry': 0,
            'document_registry': 0,
            'subscriber_info': 0
        }

    codes_list = [code for code in subscriber_codes if code]
    if not codes_list:
        return {
            'login_info': 0,
            'email_registry': 0,
            'document_registry': 0,
            'subscriber_info': 0
        }

    login_info_deleted = SubscriberLoginInfo.objects.filter(subscriberCode__in=codes_list).delete()[0]
    email_registry_deleted = 0
    document_registry_deleted = 0
    if not preserve_registry:
        email_registry_deleted = SubscriberEmailRegistry.objects.filter(subscriber_code__in=codes_list).delete()[0]
        document_registry_deleted = SubscriberDocumentRegistry.objects.filter(subscriber_code__in=codes_list).delete()[0]
    subscriber_info_deleted = SubscriberInfo.objects.filter(subscriber_code__in=codes_list).delete()[0]

    return {
        'login_info': login_info_deleted,
        'email_registry': email_registry_deleted,
        'document_registry': document_registry_deleted,
        'subscriber_info': subscriber_info_deleted
    }


def LastSubscriber():
    try:
        return ListOfSubscriber.objects.latest('code')
    except ListOfSubscriber.DoesNotExist:
        return None


def store_all_subscribers_in_chunks(data_batch, chunk_size=None):
    chunk_size = chunk_size or PanaccessConfig.DB_WRITE_CHUNK_SIZE
    total = len(data_batch)
    if total == 0:
        return (0, 0)
    
    total_inserted = 0
    total_updated = 0
    total_errors = 0
    
    for i in range(0, total, chunk_size):
        chunk = data_batch[i:i + chunk_size]
        valid_objects = []
        
        codes = {item.get('code') for item in chunk if item.get('code')}
        ids = {item.get('id') for item in chunk if item.get('id')}
        
        existing_by_code = {
            obj.code: obj for obj in ListOfSubscriber.objects.filter(code__in=codes) if obj.code
        }
        existing_by_id = {
            obj.id: obj for obj in ListOfSubscriber.objects.filter(id__in=ids) if obj.id
        }
        
        for item in chunk:
            serializer = ListOfSubscriberSerializer(data=item)
            if not serializer.is_valid():
                total_errors += 1
                continue
            
            validated = serializer.validated_data
            code = validated.get('code')
            subscriber_id = validated.get('id')

            if not code or not str(code).strip() or not subscriber_id or not str(subscriber_id).strip():
                total_errors += 1
                continue
            
            existing = None
            if code and code in existing_by_code:
                existing = existing_by_code[code]
            elif subscriber_id and subscriber_id in existing_by_id:
                existing = existing_by_id[subscriber_id]
            
            if existing:
                if _is_closure_tombstone(existing):
                    continue
                changed = False
                changed_fields = []
                for key, val in validated.items():
                    if key == "status":
                        continue
                    current_val = getattr(existing, key, None)
                    if isinstance(current_val, list) and isinstance(val, list):
                        if current_val != val:
                            setattr(existing, key, val)
                            changed = True
                            changed_fields.append(key)
                    elif isinstance(current_val, dict) and isinstance(val, dict):
                        if current_val != val:
                            setattr(existing, key, val)
                            changed = True
                            changed_fields.append(key)
                    elif str(current_val) != str(val):
                        setattr(existing, key, val)
                        changed = True
                        changed_fields.append(key)
                
                if changed:
                    try:
                        existing.save(update_fields=changed_fields)
                        total_updated += 1
                    except Exception as e:
                        logger.error("Error actualizando: %s", e)
                        total_errors += 1
            else:
                valid_objects.append(ListOfSubscriber(**validated))
        
        if valid_objects:
            try:
                created = ListOfSubscriber.objects.bulk_create(valid_objects, ignore_conflicts=True)
                total_inserted += len(created)
            except Exception as e:
                logger.error("Error bulk create: %s", e)
                total_errors += len(valid_objects)
    
    return total_inserted, total_errors


def fetch_all_subscribers(session_id=None, limit=100):
    offset = 0
    all_data = []
    parser = _get_dateutil_parser()
    
    while True:
        result = CallListExtendedSubscribers(session_id, offset, limit)
        rows = result.get("extendedSubscriberEntries") or result.get("subscriberEntries") or result.get("rows", [])
        if not rows:
            break
        
        for row in rows:
            subscriber_code = row.get("subscriberCode")
            if not subscriber_code or not str(subscriber_code).strip():
                continue
            
            subscriber_data = {
                "id": subscriber_code,
                "code": subscriber_code,
                "lastName": row.get("lastName"),
                "firstName": row.get("firstName"),
                "smartcards": row.get("smartcards"),
                "regionId": row.get("regionId"),
                "countryCode": row.get("countryCode"),
                "caf": row.get("caf"),
                "supervisor": row.get("supervisor"),
                "comment": row.get("comment"),
                "ip": row.get("ip"),
                "emails": extract_first_email(row.get("emails")),
                "phones": extract_first_phone(row.get("phones")),
                "faxes": row.get("faxes"),
                "skypes": row.get("skypes"),
                "mobiles": row.get("mobiles"),
                "custodians": row.get("custodians"),
                "address1": row.get("address1"),
                "address2": row.get("address2"),
                "address3": row.get("address3"),
                "addressCount": row.get("addressCount", 0),
                "newsletterAccepted": row.get("newsletterAccepted", False),
                "tags": row.get("tags"),
                "uniqueLogin": row.get("uniqueLogin"),
            }
            
            subscriber_data["created"] = _parse_subscriber_datetime(row.get("created"), parser)
            subscriber_data["firstOrderTime"] = _parse_subscriber_datetime(row.get("firstOrderTime"), parser)
            subscriber_data["lastExpiryTime"] = _parse_subscriber_datetime(row.get("lastExpiryTime"), parser)
            
            all_data.append(subscriber_data)
        
        offset += limit
    
    result_store = store_all_subscribers_in_chunks(all_data)
    
    if fetch_login_info_for_subscriber and all_data:
        from wind.functions.getSubscriberLoginInfo import fetch_login_info_for_codes
        codes = [item.get("code") for item in all_data if item.get("code")]
        fetch_login_info_for_codes(codes)

    return result_store


def download_subscribers_since_last(session_id=None, limit=100):
    last = LastSubscriber()
    if not last:
        return (0, 0)
    highest_code = last.code
    offset = 0
    new_data = []
    found = False
    parser = _get_dateutil_parser()
    
    while True:
        result = CallListExtendedSubscribers(session_id, offset, limit)
        rows = result.get("extendedSubscriberEntries") or result.get("subscriberEntries") or result.get("rows", [])
        if not rows:
            break
        
        for row in rows:
            code = row.get("subscriberCode")
            if not code or not str(code).strip():
                continue
            
            if code == highest_code:
                found = True
                break
            
            subscriber_data = {
                "id": code,
                "code": code,
                "lastName": row.get("lastName"),
                "firstName": row.get("firstName"),
                "smartcards": row.get("smartcards"),
                "regionId": row.get("regionId"),
                "countryCode": row.get("countryCode"),
                "caf": row.get("caf"),
                "supervisor": row.get("supervisor"),
                "comment": row.get("comment"),
                "ip": row.get("ip"),
                "emails": extract_first_email(row.get("emails")),
                "phones": extract_first_phone(row.get("phones")),
                "faxes": row.get("faxes"),
                "skypes": row.get("skypes"),
                "mobiles": row.get("mobiles"),
                "custodians": row.get("custodians"),
                "address1": row.get("address1"),
                "address2": row.get("address2"),
                "address3": row.get("address3"),
                "addressCount": row.get("addressCount", 0),
                "newsletterAccepted": row.get("newsletterAccepted", False),
                "tags": row.get("tags"),
                "uniqueLogin": row.get("uniqueLogin"),
            }
            
            subscriber_data["created"] = _parse_subscriber_datetime(row.get("created"), parser)
            subscriber_data["firstOrderTime"] = _parse_subscriber_datetime(row.get("firstOrderTime"), parser)
            subscriber_data["lastExpiryTime"] = _parse_subscriber_datetime(row.get("lastExpiryTime"), parser)
            
            new_data.append(subscriber_data)
        
        if found:
            break
        offset += limit
    
    result_store = store_all_subscribers_in_chunks(new_data)
    
    if fetch_login_info_for_subscriber and new_data:
        from wind.functions.getSubscriberLoginInfo import fetch_login_info_for_codes
        codes = [item.get("code") for item in new_data if item.get("code")]
        fetch_login_info_for_codes(codes)

    return result_store


def compare_and_update_all_subscribers(session_id=None, limit=100):
    logger.info("Reconciliando suscriptores desde PanAccess (full sync correctivo)")

    parser = _get_dateutil_parser()
    local_valid_qs = ListOfSubscriber.objects.exclude(code__isnull=True).exclude(code="")
    local_data = {obj.code: obj for obj in local_valid_qs if obj.code}
    local_total_count = len(local_data)

    remote_codes = set()
    new_subscriber_rows = []
    offset = 0
    remote_total_count = None
    total_updated = 0

    while True:
        response = CallListExtendedSubscribers(session_id, offset, limit)
        if remote_total_count is None:
            remote_total_count = int(response.get("count") or 0)

        remote_list = (
            response.get("extendedSubscriberEntries")
            or response.get("subscriberEntries")
            or response.get("rows", [])
        )
        if not remote_list:
            break

        for row in remote_list:
            code = row.get("subscriberCode")
            if not code or not str(code).strip():
                continue

            remote_codes.add(code)

            if code in local_data:
                local_obj = local_data[code]
                if _is_closure_tombstone(local_obj):
                    # No reactivar ni sobrescribir cuentas cerradas desde PanAccess.
                    continue
                if _update_subscriber_from_row(local_obj, row, parser):
                    total_updated += 1
            else:
                subscriber_data = extended_subscriber_row_to_data(row, parser)
                if subscriber_data:
                    new_subscriber_rows.append(subscriber_data)

        offset += limit
        if remote_total_count and len(remote_codes) >= remote_total_count:
            break

    total_created = 0
    create_errors = 0
    if new_subscriber_rows:
        total_created, create_errors = store_all_subscribers_in_chunks(new_subscriber_rows)

    delete_result = _delete_local_subscribers_not_in_remote(set(local_data.keys()), remote_codes)
    invalid_deleted = _cleanup_invalid_local_subscribers()

    login_deleted_not_in_remote = 0
    try:
        from wind.functions.getSubscriberLoginInfo import cleanup_login_info_not_in_remote
        login_deleted_not_in_remote = cleanup_login_info_not_in_remote(remote_codes)
    except Exception as e:
        logger.error("Error limpiando login info fuera de PanAccess: %s", e)

    return {
        "updated": total_updated,
        "created": total_created,
        "create_errors": create_errors,
        "deleted": delete_result["deleted"],
        "codes_to_delete_count": delete_result["codes_to_delete_count"],
        "preserved_closed": delete_result.get("preserved_closed", 0),
        "invalid_deleted": invalid_deleted,
        "credentials_deleted": delete_result["credentials_deleted"],
        "remote_count": len(remote_codes),
        "remote_api_count": remote_total_count,
        "local_count_before": local_total_count,
        "login_deleted_not_in_remote": login_deleted_not_in_remote,
    }


def sync_subscribers(session_id=None, limit=100):
    logger.info("Sincronizando suscriptores")

    try:
        if DataBaseEmpty():
            return fetch_all_subscribers(session_id, limit)
        else:
            return download_subscribers_since_last(session_id, limit)

    except PanAccessException as e:
        logger.error(f"Error PanAccess: {str(e)}")
        raise
    except (ConnectionError, ValueError) as e:
        logger.error(f"Error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error inesperado: {str(e)}", exc_info=True)
        raise


def _normalize_subscriber_api_answer(answer, subscriber_code: str) -> dict | None:
    if answer is None:
        return None
    if isinstance(answer, list):
        if not answer:
            return None
        answer = answer[0]
    if not isinstance(answer, dict):
        return None

    row = answer
    for key in (
        "extendedSubscriberEntry",
        "subscriberEntry",
        "subscriber",
        "entry",
        "answer",
    ):
        nested = row.get(key)
        if isinstance(nested, dict):
            row = nested
            break

    code = row.get("subscriberCode") or row.get("code") or subscriber_code
    if not code or not str(code).strip():
        return None

    normalized = dict(row)
    normalized["subscriberCode"] = str(code).strip()
    normalized.setdefault("code", normalized["subscriberCode"])
    return normalized


def CallGetSubscriber(session_id=None, subscriber_code=None):
    del session_id  # singleton

    if not subscriber_code or not str(subscriber_code).strip():
        raise ValueError("subscriber_code es requerido")

    code = str(subscriber_code).strip()
    panaccess = get_panaccess()

    attempts = (
        ("getSubscriber", {"code": code}),
        ("getSubscriber", {"subscriberCode": code}),
        ("getExtendedSubscriber", {"subscriberCode": code}),
        ("getExtendedSubscriber", {"code": code}),
    )
    last_error = None

    for api_name, parameters in attempts:
        try:
            response = panaccess.call(api_name, parameters)
            if not response.get("success"):
                last_error = response.get("errorMessage", api_name)
                continue
            row = _normalize_subscriber_api_answer(response.get("answer"), code)
            if row:
                logger.info("Suscriptor %s obtenido vía %s", code, api_name)
                return row
        except PanAccessException as exc:
            last_error = str(exc)
            logger.debug("%s no disponible para %s: %s", api_name, code, exc)

    raise PanAccessException(
        last_error or f"No se pudo obtener el suscriptor {code} por API directa"
    )


def _normalize_orders_answer(answer) -> list[dict]:
    """Normaliza la respuesta de getOrdersOfSubscriber a una lista de dicts."""
    if answer is None:
        return []
    rows = answer
    if isinstance(rows, dict):
        for key in ("orders", "Orders", "rows", "answer"):
            nested = rows.get(key)
            if isinstance(nested, list):
                rows = nested
                break
        else:
            rows = [rows] if rows else []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def CallGetOrdersOfSubscriber(session_id=None, subscriber_code=None, include_expired=True):
    """
    Trae las ordenes reales del suscriptor (getOrdersOfSubscriber): cada orden
    trae orderId, productId, sn (smartcard) y expiryTime/disabled.

    Es la fuente correcta para saber que producto tiene cada smartcard -- a
    diferencia de leer 'products'/'productEntries' de la fila de
    getSubscriber, que no esta garantizado que venga desglosado por tarjeta.
    """
    del session_id  # singleton

    if not subscriber_code or not str(subscriber_code).strip():
        raise ValueError("subscriber_code es requerido")

    code = str(subscriber_code).strip()
    panaccess = get_panaccess()

    # El resto de las llamadas del proyecto no usan el prefijo "cv"
    # (getSubscriber, addSubscriber, addLicenseBlockToSubscriber, etc.), y la
    # cuenta de servicio confirmó en pruebas reales que NO tiene permiso para
    # la variante "cv" de esta función -- se prueba primero sin prefijo,
    # igual que las demás, y "cv..." queda solo como respaldo por si algún
    # otro entorno sí lo tiene habilitado.
    attempts = (
        ("getOrdersOfSubscriber", {"subscriberCode": code, "includeExpiredOrders": include_expired}),
        ("cvGetOrdersOfSubscriber", {"subscriberCode": code, "includeExpiredOrders": include_expired}),
    )
    last_error = None

    for api_name, parameters in attempts:
        try:
            response = panaccess.call(api_name, parameters)
            if not response.get("success"):
                last_error = response.get("errorMessage", api_name)
                continue
            orders = _normalize_orders_answer(response.get("answer"))
            logger.info("Ordenes de %s obtenidas vía %s (%d)", code, api_name, len(orders))
            return orders
        except PanAccessException as exc:
            last_error = str(exc)
            logger.debug("%s no disponible para %s: %s", api_name, code, exc)

    logger.warning(
        "No se pudieron obtener las órdenes de %s (%s); se continúa sin ellas",
        code,
        last_error,
    )
    return []


def CallListExtendedSubscribers(session_id=None, offset=0, limit=100):
    try:
        panaccess = get_panaccess()
        parameters = {
            'usePrefixFlags': True,
            'offset': offset,
            'limit': limit,
            "orderBy": "created",
            "orderDir": "DESC"
        }
        response = panaccess.call('getListOfExtendedSubscribers', parameters)

        if response.get('success'):
            return response.get('answer', {})
        else:
            error_message = response.get('errorMessage', 'Error desconocido al obtener suscriptores extendidos')
            logger.error(f"Error PanAccess: {error_message}")
            raise PanAccessException(error_message)

    except PanAccessException:
        raise
    except Exception as e:
        logger.error(f"Error llamada API: {str(e)}", exc_info=True)
        raise
