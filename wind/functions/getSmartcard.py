"""
Funciones para obtener y sincronizar smartcards desde PanAccess.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import transaction

from appConfig import PanaccessConfig
from wind.models import ListOfSmartcards, ListOfSubscriber
from wind.serializers import ListOfSmartcardsSerializer

from wind.services import get_panaccess
from wind.exceptions import PanAccessException

logger = logging.getLogger(__name__)

_CHAR_FIELD_MAX_LENGTHS: dict[str, int] | None = None


def _smartcard_char_max_lengths() -> dict[str, int]:
    global _CHAR_FIELD_MAX_LENGTHS
    if _CHAR_FIELD_MAX_LENGTHS is None:
        _CHAR_FIELD_MAX_LENGTHS = {
            f.name: f.max_length
            for f in ListOfSmartcards._meta.get_fields()
            if getattr(f, "max_length", None)
        }
    return _CHAR_FIELD_MAX_LENGTHS


def normalize_smartcard_row(item: dict) -> dict:
    """Filtra campos del modelo y trunca strings que excedan max_length."""
    model_fields = {f.name for f in ListOfSmartcards._meta.get_fields()}
    normalized = {k: v for k, v in item.items() if k in model_fields}
    for key, max_len in _smartcard_char_max_lengths().items():
        value = normalized.get(key)
        if value is None:
            continue
        text = str(value)
        if len(text) > max_len:
            logger.warning(
                "Smartcard %s: campo '%s' truncado (%s -> %s caracteres)",
                normalized.get("sn"),
                key,
                len(text),
                max_len,
            )
            normalized[key] = text[:max_len]
    return normalized


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
    
    for i in range(0, total, chunk_size):
        chunk = data_batch[i:i + chunk_size]
        try:
            registros = []
            for item in chunk:
                filtered_item = normalize_smartcard_row(item)
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


def _subscriber_code_filters(subscriber_code: str) -> dict:
    code = str(subscriber_code).strip()
    return {
        "groupOp": "AND",
        "rules": [{"field": "subscriberCode", "op": "eq", "data": code}],
    }


def _fetch_smartcards_for_subscriber(
    session_id=None,
    subscriber_code: str = "",
    limit: int = 100,
) -> list[dict]:
    """Descarga smartcards de un abonado usando filters.subscriberCode en PanAccess."""
    code = str(subscriber_code).strip()
    if not code:
        return []

    offset = 0
    entries: list[dict] = []
    remote_total_count = None
    max_pages = PanaccessConfig.SMARTCARD_SUBSCRIBER_SYNC_MAX_PAGES

    for _ in range(max_pages):
        response = CallListSmartcards(
            session_id,
            offset,
            limit,
            subscriber_code=code,
            order_by_sn=False,
        )
        if remote_total_count is None:
            remote_total_count = int(response.get("count") or 0)

        batch = response.get("smartcardEntries") or []
        if not batch:
            break

        for entry in batch:
            if not isinstance(entry, dict) or not entry.get("sn"):
                continue
            sub = entry.get("subscriberCode")
            if sub and str(sub).strip() != code:
                continue
            entries.append(entry)

        offset += limit
        if len(batch) < limit:
            break
        if remote_total_count and offset >= remote_total_count:
            break

    return entries


def _reconcile_subscriber_smartcards(
    subscriber_code: str,
    remote_list: list[dict],
) -> dict:
    """Compara smartcards remotas de un abonado con las locales y aplica cambios."""
    code = str(subscriber_code).strip()
    local_by_sn = {
        obj.sn: obj
        for obj in ListOfSmartcards.objects.filter(subscriberCode=code).exclude(
            sn__isnull=True
        ).exclude(sn="")
        if obj.sn
    }

    remote_sns: set[str] = set()
    new_rows: list[dict] = []
    total_updated = 0

    for remote in remote_list:
        if not isinstance(remote, dict):
            continue
        remote = normalize_smartcard_row(remote)
        sn = remote.get("sn")
        if not sn or not str(sn).strip():
            continue
        sn = str(sn).strip()
        remote_sns.add(sn)

        if not remote.get("subscriberCode"):
            remote["subscriberCode"] = code

        if sn in local_by_sn:
            changed_fields = _update_smartcard_from_remote(local_by_sn[sn], remote)
            if changed_fields:
                try:
                    local_by_sn[sn].save(update_fields=changed_fields)
                    total_updated += 1
                except Exception as e:
                    logger.error(
                        "Error actualizando smartcard SN %s (abonado %s): %s",
                        sn,
                        code,
                        e,
                    )
            continue

        existing = ListOfSmartcards.objects.filter(sn=sn).first()
        if existing:
            changed_fields = _update_smartcard_from_remote(existing, remote)
            if changed_fields:
                try:
                    existing.save(update_fields=changed_fields)
                    total_updated += 1
                except Exception as e:
                    logger.error(
                        "Error actualizando smartcard SN %s (abonado %s): %s",
                        sn,
                        code,
                        e,
                    )
        else:
            new_rows.append(remote)

    total_created = 0
    if new_rows:
        before = ListOfSmartcards.objects.filter(subscriberCode=code).count()
        store_all_smartcards_in_chunks(new_rows)
        total_created = max(
            0,
            ListOfSmartcards.objects.filter(subscriberCode=code).count() - before,
        )

    extra_sns = set(local_by_sn.keys()) - remote_sns
    deleted = 0
    if extra_sns:
        deleted = ListOfSmartcards.objects.filter(
            subscriberCode=code,
            sn__in=extra_sns,
        ).delete()[0]

    return {
        "updated": total_updated,
        "created": total_created,
        "deleted": deleted,
        "remote_count": len(remote_sns),
    }


def _process_subscriber_smartcard_sync(
    subscriber_code: str,
    session_id=None,
    limit: int = 100,
) -> dict:
    remote_list = _fetch_smartcards_for_subscriber(session_id, subscriber_code, limit)
    stats = _reconcile_subscriber_smartcards(subscriber_code, remote_list)
    stats["subscriber_code"] = subscriber_code
    return stats


def compare_and_update_smartcards_by_subscribers(session_id=None, limit=100):
    """Reconcilia smartcards consultando PanAccess filtrado por cada abonado local."""
    subscriber_codes = list(
        ListOfSubscriber.objects.exclude(code__isnull=True)
        .exclude(code="")
        .values_list("code", flat=True)
    )
    local_count_before = ListOfSmartcards.objects.count()
    page_limit = max(1, min(int(limit or 100), 1000))

    logger.info(
        "Reconciliando smartcards por abonado — abonados=%s, limit=%s",
        len(subscriber_codes),
        page_limit,
    )

    totals = {
        "updated": 0,
        "created": 0,
        "deleted": 0,
        "remote_count": 0,
        "subscribers_processed": 0,
        "subscribers_failed": 0,
    }
    concurrency = PanaccessConfig.SMARTCARD_SUBSCRIBER_CONCURRENCY

    def _merge_stats(stats: dict) -> None:
        totals["updated"] += stats.get("updated", 0)
        totals["created"] += stats.get("created", 0)
        totals["deleted"] += stats.get("deleted", 0)
        totals["remote_count"] += stats.get("remote_count", 0)
        totals["subscribers_processed"] += 1

    if concurrency <= 1 or len(subscriber_codes) <= 1:
        for code in subscriber_codes:
            try:
                stats = _process_subscriber_smartcard_sync(code, session_id, page_limit)
                _merge_stats(stats)
            except Exception as exc:
                totals["subscribers_failed"] += 1
                logger.error(
                    "Error sincronizando smartcards del abonado %s: %s",
                    code,
                    exc,
                )
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    _process_subscriber_smartcard_sync,
                    code,
                    session_id,
                    page_limit,
                ): code
                for code in subscriber_codes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    stats = future.result()
                    _merge_stats(stats)
                except Exception as exc:
                    totals["subscribers_failed"] += 1
                    logger.error(
                        "Error sincronizando smartcards del abonado %s: %s",
                        code,
                        exc,
                    )

    logger.info(
        "Reconciliación smartcards por abonado — abonados=%s, fallidos=%s, "
        "remoto=%s, local antes=%s, actualizados=%s, creados=%s, eliminados=%s",
        totals["subscribers_processed"],
        totals["subscribers_failed"],
        totals["remote_count"],
        local_count_before,
        totals["updated"],
        totals["created"],
        totals["deleted"],
    )

    return {
        "strategy": "by_subscriber",
        "updated": totals["updated"],
        "created": totals["created"],
        "deleted": totals["deleted"],
        "remote_count": totals["remote_count"],
        "local_count_before": local_count_before,
        "subscribers_total": len(subscriber_codes),
        "subscribers_processed": totals["subscribers_processed"],
        "subscribers_failed": totals["subscribers_failed"],
    }


def _compare_and_update_all_smartcards_full(session_id=None, limit=100):
    logger.info("Reconciliando smartcards desde PanAccess (full scan)")
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
            remote = normalize_smartcard_row(remote)
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
                if remote.get("sn"):
                    new_rows.append(remote)

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
        "strategy": "full_scan",
        "updated": total_updated,
        "created": total_created,
        "deleted": deleted,
        "codes_to_delete_count": len(extra_sns),
        "remote_count": len(remote_sns),
        "remote_api_count": remote_total_count,
        "local_count_before": local_count_before,
    }


def compare_and_update_all_smartcards(session_id=None, limit=100, *, force_full: bool = False):
    if force_full or not PanaccessConfig.SMARTCARD_SYNC_BY_SUBSCRIBER:
        return _compare_and_update_all_smartcards_full(session_id, limit)
    return compare_and_update_smartcards_by_subscribers(session_id, limit)


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
    *,
    order_by_sn: bool = True,
    filters: dict | None = None,
    new_filters: dict | None = None,
):
    try:
        panaccess = get_panaccess()
        parameters: dict = {
            "offset": offset,
            "limit": limit,
        }
        if order_by_sn:
            parameters["orderDir"] = "DESC"
            parameters["orderBy"] = "sn"
        if subscriber_code:
            code = str(subscriber_code).strip()
            parameters["filters"] = _subscriber_code_filters(code)
            # Alias legacy que algunos despliegues aún aceptan como atajo.
            parameters["subscriberCode"] = code
            parameters["code"] = code
        if filters:
            parameters["filters"] = filters
        if new_filters:
            parameters["newFilters"] = new_filters

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
