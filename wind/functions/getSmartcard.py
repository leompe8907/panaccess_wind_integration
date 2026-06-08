"""
Funciones para obtener y sincronizar smartcards desde PanAccess.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import transaction

from appConfig import PanaccessConfig
from wind.models import ListOfSmartcards
from wind.serializers import ListOfSmartcardsSerializer

from wind.services import get_panaccess
from wind.exceptions import PanAccessException

logger = logging.getLogger(__name__)


def DataBaseEmpty():
    """
    Verifica si la tabla ListOfSmartcards está vacía.
    """
    return not ListOfSmartcards.objects.exists()


def LastSmartcard():
    """
    Retorna la última smartcard registrada en la base de datos según el campo 'sn'.
    """
    try:
        return ListOfSmartcards.objects.latest('sn')
    except ListOfSmartcards.DoesNotExist:
        return None


def store_all_smartcards_in_chunks(data_batch, chunk_size=100):
    """
    Almacena smartcards en la base de datos en bloques para mejorar el rendimiento.
    """
    total = len(data_batch)
    if total == 0:
        return
    logger.info(f"Almacenando {total} smartcards")
    
    model_fields = {f.name for f in ListOfSmartcards._meta.get_fields()}
    
    for i in range(0, total, chunk_size):
        chunk = data_batch[i:i + chunk_size]
        try:
            registros = []
            for item in chunk:
                filtered_item = {k: v for k, v in item.items() if k in model_fields}
                if filtered_item.get('sn'):
                    registros.append(ListOfSmartcards(**filtered_item))
            
            if registros:
                ListOfSmartcards.objects.bulk_create(registros, ignore_conflicts=True)
        except Exception as e:
            logger.error(f"Error insertando chunk {i//chunk_size + 1}: {str(e)}")
    logger.info(f"Almacenados {total} smartcards")


def fetch_all_smartcards(session_id=None, limit=100):
    logger.info("Descarga completa de smartcards")
    offset = 0
    all_data = []
    
    while True:
        result = CallListSmartcards(session_id, offset, limit)
        smartcard_entries = result.get("smartcardEntries", [])
        if not smartcard_entries:
            break
        
        for entry in smartcard_entries:
            if not isinstance(entry, dict) or 'sn' not in entry:
                continue
            all_data.append(entry)
        
        offset += limit
    
    logger.info(f"Descargados {len(all_data)} smartcards")
    return store_all_smartcards_in_chunks(all_data)


def download_smartcards_since_last(session_id=None, limit=100):
    last = LastSmartcard()
    if not last:
        return []
    
    highest_sn = last.sn
    logger.info(f"Descarga incremental desde SN: {highest_sn}")
    offset = 0
    new_data = []
    found = False
    
    while True:
        result = CallListSmartcards(session_id, offset, limit)
        smartcard_entries = result.get("smartcardEntries", [])
        if not smartcard_entries:
            break
        
        for entry in smartcard_entries:
            if not isinstance(entry, dict) or 'sn' not in entry:
                continue
            
            sn = entry.get('sn')
            if sn == highest_sn:
                found = True
                break
            new_data.append(entry)
        
        if found:
            break
        offset += limit
    
    logger.info(f"Nuevos smartcards descargados: {len(new_data)}")
    return store_all_smartcards_in_chunks(new_data)


def _smartcard_model_fields():
    return {f.name for f in ListOfSmartcards._meta.get_fields()}


def _update_smartcard_from_remote(local_obj, remote: dict) -> list[str]:
    changed_fields = []
    for key, val in remote.items():
        if not hasattr(local_obj, key):
            continue
        local_val = getattr(local_obj, key)
        if isinstance(local_val, list) and isinstance(val, list):
            if local_val != val:
                setattr(local_obj, key, val)
                changed_fields.append(key)
        elif str(local_val) != str(val):
            setattr(local_obj, key, val)
            changed_fields.append(key)
    return changed_fields


def compare_and_update_all_smartcards(session_id=None, limit=100):
    logger.info("Reconciliando smartcards desde PanAccess")
    model_fields = _smartcard_model_fields()
    local_data = {
        obj.sn: obj
        for obj in ListOfSmartcards.objects.exclude(sn__isnull=True).exclude(sn="")
        if obj.sn
    }
    local_count_before = len(local_data)
    remote_sns = set()
    new_rows = []
    offset = 0
    remote_total_count = None
    total_updated = 0

    while True:
        response = CallListSmartcards(session_id, offset, limit)
        if remote_total_count is None:
            remote_total_count = int(response.get("count") or 0)

        remote_list = response.get("smartcardEntries", []) or []
        if not remote_list:
            break

        for remote in remote_list:
            if not isinstance(remote, dict):
                continue
            sn = remote.get("sn")
            if not sn or not str(sn).strip():
                continue
            sn = str(sn).strip()
            remote_sns.add(sn)

            if sn in local_data:
                changed_fields = _update_smartcard_from_remote(local_data[sn], remote)
                if changed_fields:
                    try:
                        local_data[sn].save(update_fields=changed_fields)
                        total_updated += 1
                    except Exception as e:
                        logger.error("Error actualizando smartcard SN %s: %s", sn, e)
            else:
                filtered = {k: v for k, v in remote.items() if k in model_fields}
                if filtered.get("sn"):
                    new_rows.append(filtered)

        offset += limit
        if remote_total_count and len(remote_sns) >= remote_total_count:
            break

    total_created = 0
    if new_rows:
        before = ListOfSmartcards.objects.count()
        store_all_smartcards_in_chunks(new_rows)
        total_created = max(0, ListOfSmartcards.objects.count() - before)

    extra_sns = set(local_data.keys()) - remote_sns
    deleted = 0
    if extra_sns:
        deleted = ListOfSmartcards.objects.filter(sn__in=extra_sns).delete()[0]

    logger.info(
        "Reconciliación smartcards — remoto=%s, local antes=%s, actualizados=%s, creados=%s, eliminados=%s",
        len(remote_sns),
        local_count_before,
        total_updated,
        total_created,
        deleted,
    )

    return {
        "updated": total_updated,
        "created": total_created,
        "deleted": deleted,
        "codes_to_delete_count": len(extra_sns),
        "remote_count": len(remote_sns),
        "remote_api_count": remote_total_count,
        "local_count_before": local_count_before,
    }


def sync_smartcards(session_id=None, limit=100):
    logger.info("Sincronizando smartcards")
    try:
        if DataBaseEmpty():
            return fetch_all_smartcards(session_id, limit)
        return compare_and_update_all_smartcards(session_id, limit)
    except PanAccessException as e:
        logger.error(f"Error PanAccess: {str(e)}")
        raise
    except (ConnectionError, ValueError) as e:
        logger.error(f"Error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error inesperado: {str(e)}", exc_info=True)
        raise


def _normalize_smartcard_api_answer(answer, sn: str | None = None) -> dict | None:
    if answer is None:
        return None
    if isinstance(answer, list):
        if not answer:
            return None
        answer = answer[0]
    if not isinstance(answer, dict):
        return None

    row = answer
    for key in ("smartcardEntry", "smartcard", "entry", "answer"):
        nested = row.get(key)
        if isinstance(nested, dict):
            row = nested
            break

    serial = row.get("sn") or row.get("serialNumber") or sn
    if not serial or not str(serial).strip():
        return None

    normalized = dict(row)
    normalized["sn"] = str(serial).strip()
    return normalized


def CallGetSmartcard(session_id=None, sn=None):
    del session_id

    if not sn or not str(sn).strip():
        raise ValueError("sn es requerido")

    serial = str(sn).strip()
    panaccess = get_panaccess()
    attempts = (
        ("getSmartcard", {"sn": serial}),
        ("getSmartcard", {"serialNumber": serial}),
        ("getSmartcardBySn", {"sn": serial}),
        ("getSmartcardBySerialNumber", {"sn": serial}),
    )
    last_error = None

    for api_name, parameters in attempts:
        try:
            response = panaccess.call(api_name, parameters)
            if not response.get("success"):
                last_error = response.get("errorMessage", api_name)
                continue
            row = _normalize_smartcard_api_answer(response.get("answer"), serial)
            if row:
                logger.debug("Smartcard %s obtenida vía %s", serial, api_name)
                return row
        except PanAccessException as exc:
            last_error = str(exc)
            logger.debug("%s no disponible para SN %s: %s", api_name, serial, exc)

    raise PanAccessException(
        last_error or f"No se pudo obtener smartcard {serial}"
    )


def CallListSmartcards(
    session_id=None,
    offset=0,
    limit=100,
    subscriber_code: str | None = None,
):
    try:
        panaccess = get_panaccess()
        parameters = {
            "offset": offset,
            "limit": limit,
            "orderDir": "DESC",
            "orderBy": "sn",
        }
        if subscriber_code:
            code = str(subscriber_code).strip()
            parameters["subscriberCode"] = code
            parameters["code"] = code

        response = panaccess.call("getListOfSmartcards", parameters)

        if response.get('success'):
            return response.get('answer', {})
        else:
            error_message = response.get('errorMessage', 'Error desconocido al obtener smartcards')
            logger.error(f"Error PanAccess: {error_message}")
            raise PanAccessException(error_message)
    except PanAccessException:
        raise
    except Exception as e:
        logger.error(f"Error llamada API: {str(e)}", exc_info=True)
        raise


def _fetch_one_smartcard_by_sn(sn: str) -> dict | None:
    try:
        return CallGetSmartcard(sn=sn)
    except Exception as exc:
        logger.debug("getSmartcard falló para %s: %s", sn, exc)
        return None


def fetch_subscriber_smartcards_from_panaccess(
    subscriber_code: str,
    target_sns: list[str] | None = None,
    *,
    profile_mode: bool = True,
) -> dict:
    code = str(subscriber_code).strip() if subscriber_code else ""
    target_set = {str(s).strip() for s in (target_sns or []) if s and str(s).strip()}
    fetched_sns: set[str] = set()
    entries: list[dict] = []

    max_pages_subscriber = PanaccessConfig.SMARTCARD_SUBSCRIBER_MAX_PAGES
    page_limit = PanaccessConfig.SMARTCARD_PAGE_LIMIT

    if code:
        offset = 0
        for _ in range(max_pages_subscriber):
            try:
                answer = CallListSmartcards(
                    offset=offset,
                    limit=page_limit,
                    subscriber_code=code,
                )
            except PanAccessException as exc:
                logger.warning(
                    "Listado smartcards por abonado %s falló (offset=%s): %s",
                    code,
                    offset,
                    exc,
                )
                break

            batch = answer.get("smartcardEntries") or []
            if not batch:
                break

            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                sn = entry.get("sn")
                if not sn:
                    continue
                sn = str(sn).strip()
                sub = entry.get("subscriberCode")
                if sub and str(sub).strip() != code and sn not in target_set:
                    continue
                entries.append(entry)
                fetched_sns.add(sn)

            if len(batch) < page_limit:
                break
            offset += page_limit

    missing_sns = [sn for sn in target_set if sn not in fetched_sns]
    if missing_sns:
        workers = max(1, PanaccessConfig.SMARTCARD_SN_CONCURRENCY)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_fetch_one_smartcard_by_sn, sn): sn
                for sn in missing_sns
            }
            for future in as_completed(futures):
                row = future.result()
                if row:
                    if code and not row.get("subscriberCode"):
                        row["subscriberCode"] = code
                    entries.append(row)
                    if row.get("sn"):
                        fetched_sns.add(str(row["sn"]).strip())

    use_global = PanaccessConfig.SMARTCARD_GLOBAL_FALLBACK
    if not profile_mode:
        use_global = True

    global_saved = 0
    if use_global and code and not entries:
        max_pages = PanaccessConfig.SMARTCARD_SYNC_MAX_PAGES
        offset = 0
        for _ in range(max_pages):
            try:
                answer = CallListSmartcards(offset=offset, limit=page_limit)
            except PanAccessException:
                break
            batch = answer.get("smartcardEntries") or []
            if not batch:
                break
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                sn = entry.get("sn")
                sub = entry.get("subscriberCode")
                if sub == code or (sn and sn in target_set):
                    entries.append(entry)
                    global_saved += 1
            if len(batch) < page_limit:
                break
            offset += page_limit

    return {
        "subscriber_code": code,
        "entries": entries,
        "fetched_sns": len(fetched_sns),
        "target_sns": len(target_set),
        "global_fallback": use_global and global_saved > 0,
        "global_entries": global_saved,
    }
