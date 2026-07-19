"""
Funciones para obtener y sincronizar smartcards desde PanAccess.
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone as dt_timezone

from django.db import close_old_connections, transaction
from django.utils import timezone

from appConfig import PanaccessConfig, RedisConfig
from concurrent.futures import ThreadPoolExecutor, as_completed

from wind.models import ListOfSmartcards, ListOfSubscriber
from wind.serializers import ListOfSmartcardsSerializer

from wind.services import get_panaccess
from wind.exceptions import PanAccessException

logger = logging.getLogger(__name__)

_CHAR_FIELD_MAX_LENGTHS: dict[str, int] | None = None
_DATETIME_FIELD_NAMES: frozenset[str] | None = None


def _smartcard_char_max_lengths() -> dict[str, int]:
    global _CHAR_FIELD_MAX_LENGTHS
    if _CHAR_FIELD_MAX_LENGTHS is None:
        _CHAR_FIELD_MAX_LENGTHS = {
            f.name: f.max_length
            for f in ListOfSmartcards._meta.get_fields()
            if getattr(f, "max_length", None)
        }
    return _CHAR_FIELD_MAX_LENGTHS


def _smartcard_datetime_field_names() -> frozenset[str]:
    global _DATETIME_FIELD_NAMES
    if _DATETIME_FIELD_NAMES is None:
        from django.db import models as dj_models

        _DATETIME_FIELD_NAMES = frozenset(
            f.name
            for f in ListOfSmartcards._meta.get_fields()
            if isinstance(f, dj_models.DateTimeField)
        )
    return _DATETIME_FIELD_NAMES


def _ensure_aware_datetime(value):
    """Convierte fechas naive/string de PanAccess a datetime aware (UTC)."""
    if value is None or value == "":
        return None

    from datetime import datetime

    if isinstance(value, str):
        try:
            from dateutil import parser as date_parser

            value = date_parser.parse(value)
        except Exception:
            from django.utils.dateparse import parse_datetime

            value = parse_datetime(value)
            if value is None:
                return None

    if not isinstance(value, datetime):
        return value

    if timezone.is_naive(value):
        return timezone.make_aware(value, dt_timezone.utc)
    return value


def normalize_smartcard_row(item: dict) -> dict:
    """Filtra campos del modelo, normaliza datetimes y trunca strings largos."""
    model_fields = {f.name for f in ListOfSmartcards._meta.get_fields()}
    normalized = {k: v for k, v in item.items() if k in model_fields}
    for key in _smartcard_datetime_field_names():
        if key in normalized:
            normalized[key] = _ensure_aware_datetime(normalized.get(key))
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


def store_all_smartcards_in_chunks(data_batch, chunk_size=None):
    """
    Almacena smartcards en la base de datos en bloques para mejorar el rendimiento.
    """
    chunk_size = chunk_size or PanaccessConfig.DB_WRITE_CHUNK_SIZE
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


def _format_panaccess_filter_datetime(dt) -> str:
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_sync_timestamp(raw: str | None):
    if not raw:
        return None
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed


def _resolve_incremental_since():
    stored = RedisConfig.get_smartcard_incremental_since()
    parsed = _parse_sync_timestamp(stored)
    if parsed is not None:
        return parsed
    lookback = PanaccessConfig.SMARTCARD_INCREMENTAL_LOOKBACK_HOURS
    return timezone.now() - timedelta(hours=lookback)


def _smartcards_changed_since_filters(since_label: str) -> dict:
    """Filtro OR: tarjetas con lastContact o lastActivation posteriores a since_label."""
    return {
        "groupOp": "OR",
        "rules": [
            {"field": "lastContact", "op": "gt", "data": since_label},
            {"field": "lastActivation", "op": "gt", "data": since_label},
        ],
    }


def _valid_subscriber_codes() -> set[str]:
    return {
        str(code).strip()
        for code in ListOfSubscriber.objects.exclude(code__isnull=True)
        .exclude(code="")
        .values_list("code", flat=True)
        if code and str(code).strip()
    }


def _fetch_smartcards_changed_since(
    session_id=None,
    since_label: str = "",
    limit: int = 100,
) -> list[dict]:
    if not since_label:
        return []

    offset = 0
    entries: list[dict] = []
    remote_total_count = None
    max_pages = PanaccessConfig.SMARTCARD_INCREMENTAL_MAX_PAGES
    filters = _smartcards_changed_since_filters(since_label)
    page_num = 0

    while True:
        if max_pages > 0 and page_num >= max_pages:
            logger.warning(
                "Sync incremental smartcards: tope de %s páginas alcanzado "
                "(offset=%s, count=%s). Suba PANACCESS_SMARTCARD_INCREMENTAL_MAX_PAGES "
                "o use 0 para sin límite.",
                max_pages,
                offset,
                remote_total_count,
            )
            break

        response = CallListSmartcards(
            session_id,
            offset,
            limit,
            order_by_sn=False,
            filters=filters,
        )
        if remote_total_count is None:
            remote_total_count = int(response.get("count") or 0)

        batch = response.get("smartcardEntries") or []
        if not batch:
            break

        for entry in batch:
            if isinstance(entry, dict) and entry.get("sn"):
                entries.append(entry)

        page_num += 1
        offset += limit
        if len(batch) < limit:
            break
        if remote_total_count and offset >= remote_total_count:
            break

    return entries


def _upsert_smartcard_batch(
    remote_list: list[dict],
    *,
    valid_subscriber_codes: set[str] | None = None,
) -> dict:
    updated = 0
    skipped = 0
    new_rows: list[dict] = []
    remote_sns: set[str] = set()

    for remote in remote_list:
        if not isinstance(remote, dict):
            continue
        remote = normalize_smartcard_row(remote)
        sn = remote.get("sn")
        if not sn or not str(sn).strip():
            continue
        sn = str(sn).strip()

        sub = remote.get("subscriberCode")
        sub_code = str(sub).strip() if sub else ""
        if valid_subscriber_codes is not None:
            if not sub_code or sub_code not in valid_subscriber_codes:
                skipped += 1
                continue

        remote_sns.add(sn)
        existing = ListOfSmartcards.objects.filter(sn=sn).first()
        if existing:
            changed_fields = _update_smartcard_from_remote(existing, remote)
            if changed_fields:
                try:
                    existing.save(update_fields=changed_fields)
                    updated += 1
                except Exception as exc:
                    logger.error("Error actualizando smartcard SN %s: %s", sn, exc)
        else:
            new_rows.append(remote)

    created = 0
    if new_rows:
        before = ListOfSmartcards.objects.count()
        store_all_smartcards_in_chunks(new_rows)
        created = max(0, ListOfSmartcards.objects.count() - before)

    return {
        "updated": updated,
        "created": created,
        "deleted": 0,
        "skipped": skipped,
        "remote_count": len(remote_sns),
    }


def compare_and_update_smartcards_incremental(session_id=None, limit=100):
    """Actualiza smartcards que cambiaron desde el último sync (lastContact/lastActivation)."""
    sync_marker = timezone.now()
    since = _resolve_incremental_since()
    since_label = _format_panaccess_filter_datetime(since)
    page_limit = max(1, min(int(limit or 100), 1000))
    valid_codes = _valid_subscriber_codes()

    logger.info(
        "Sync incremental smartcards — since=%s, abonados_locales=%s, limit=%s",
        since_label,
        len(valid_codes),
        page_limit,
    )

    remote_list = _fetch_smartcards_changed_since(session_id, since_label, page_limit)
    stats = _upsert_smartcard_batch(remote_list, valid_subscriber_codes=valid_codes)
    stats.update(
        {
            "strategy": "incremental",
            "since": since_label,
            "fetched": len(remote_list),
            "sync_marker": sync_marker.isoformat(),
        }
    )

    RedisConfig.set_smartcard_incremental_since(sync_marker.isoformat())
    logger.info(
        "Sync incremental smartcards — fetched=%s, remoto=%s, actualizados=%s, "
        "creados=%s, omitidos=%s",
        len(remote_list),
        stats.get("remote_count"),
        stats.get("updated"),
        stats.get("created"),
        stats.get("skipped"),
    )
    return stats


def _should_run_full_smartcard_by_subscriber() -> bool:
    if not PanaccessConfig.SMARTCARD_SYNC_BY_SUBSCRIBER:
        return False
    if PanaccessConfig.SMARTCARD_PIPELINE_COMPLETE_EACH_CYCLE:
        return True

    every_hours = PanaccessConfig.SMARTCARD_FULL_BY_SUBSCRIBER_EVERY_HOURS
    if every_hours <= 0:
        return False

    last = _parse_sync_timestamp(RedisConfig.get_smartcard_full_by_subscriber_at())
    if last is None:
        return True
    return timezone.now() - last >= timedelta(hours=every_hours)


def run_smartcard_sync_for_pipeline(
    session_id=None,
    limit=100,
    *,
    force_full: bool = False,
) -> dict:
    """
    Estrategia híbrida para el pipeline periódico:
    - incremental: cambios recientes (paginación completa si MAX_PAGES=0)
    - por abonado: reconciliación completa cada ciclo si PIPELINE_COMPLETE_EACH_CYCLE
    """
    if force_full:
        result = compare_and_update_all_smartcards(session_id, limit, force_full=True)
        return {"strategy": "force_full", "result": result}

    payload: dict = {"strategy": "pipeline_hybrid", "steps": {}}

    if PanaccessConfig.SMARTCARD_SYNC_INCREMENTAL:
        payload["steps"]["incremental"] = compare_and_update_smartcards_incremental(
            session_id, limit
        )

    run_full_by_sub = _should_run_full_smartcard_by_subscriber()
    if run_full_by_sub:
        logger.info("Pipeline smartcards — reconciliación completa por abonado")
        payload["steps"]["by_subscriber"] = compare_and_update_smartcards_by_subscribers(
            session_id, limit
        )
        RedisConfig.set_smartcard_full_by_subscriber_at(timezone.now().isoformat())
    elif not PanaccessConfig.SMARTCARD_SYNC_INCREMENTAL:
        payload["steps"]["by_subscriber"] = compare_and_update_smartcards_by_subscribers(
            session_id, limit
        )

    if not payload["steps"]:
        payload["steps"]["by_subscriber"] = compare_and_update_smartcards_by_subscribers(
            session_id, limit
        )

    return payload


def _fetch_smartcards_for_subscriber(
    session_id=None,
    subscriber_code: str = "",
    limit: int = 100,
) -> tuple[list[dict], bool]:
    """
    Descarga smartcards de un abonado usando filters.subscriberCode en
    PanAccess.

    Devuelve (entries, truncated). truncated=True significa que NO se llegó
    a ver el catálogo completo de ese abonado (se cortó por el tope de
    páginas) -- el llamador no debe usar "sn no visto" como sinónimo de
    "sn ya no existe" cuando esto pasa, porque borraría smartcards válidas
    que simplemente no entraron en el muestreo.
    """
    code = str(subscriber_code).strip()
    if not code:
        return [], False

    offset = 0
    entries: list[dict] = []
    remote_total_count = None
    max_pages = PanaccessConfig.SMARTCARD_SUBSCRIBER_SYNC_MAX_PAGES
    page_num = 0
    truncated = False

    while True:
        if max_pages > 0 and page_num >= max_pages:
            # Si ya vimos todo lo que el propio PanAccess dijo que había
            # (remote_total_count), el tope se alcanzó justo al terminar --
            # no es un corte real. Si no, sí quedó contenido sin ver.
            truncated = bool(remote_total_count) and len(entries) < remote_total_count
            if truncated:
                logger.warning(
                    "Smartcards abonado %s: tope de %s páginas alcanzado con solo "
                    "%s/%s vistas -- NO se borrarán huérfanas en esta corrida para "
                    "no eliminar smartcards válidas que quedaron fuera del muestreo. "
                    "Use PANACCESS_SMARTCARD_SUBSCRIBER_SYNC_MAX_PAGES=0 para sin límite.",
                    code,
                    max_pages,
                    len(entries),
                    remote_total_count,
                )
            break

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

        page_num += 1
        offset += limit
        if len(batch) < limit:
            break
        if remote_total_count and offset >= remote_total_count:
            break

    return entries, truncated


def _reconcile_subscriber_smartcards(
    subscriber_code: str,
    remote_list: list[dict],
    *,
    truncated: bool = False,
) -> dict:
    """
    Compara smartcards remotas de un abonado con las locales y aplica
    cambios. Si `truncated=True` (no se vio el catálogo remoto completo de
    este abonado), se aplican altas/actualizaciones igual -- son seguras,
    vienen de datos reales que sí se vieron -- pero se omite el borrado de
    huérfanas, porque "no visto" no es lo mismo que "ya no existe" cuando
    la paginación se cortó antes de tiempo.
    """
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

    deleted = 0
    skipped_deletion = False
    if truncated:
        skipped_deletion = True
    else:
        extra_sns = set(local_by_sn.keys()) - remote_sns
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
        "truncated": truncated,
        "skipped_deletion": skipped_deletion,
    }


def _process_subscriber_smartcard_sync(
    subscriber_code: str,
    session_id=None,
    limit: int = 100,
) -> dict:
    # Corre dentro de un ThreadPoolExecutor (ver compare_and_update_smartcards_
    # by_subscribers). Django abre una conexión de BD nueva por hilo la primera
    # vez que se usa el ORM ahí, pero como esos hilos no son el hilo principal
    # de un request/tarea Celery, Django nunca la cierra sola al terminar --
    # bajo sync sostenido con muchos abonados eso puede ir acumulando
    # conexiones abiertas/inactivas en Postgres. close_old_connections() al
    # terminar cada tarea del hilo libera la conexión si ya expiró
    # (CONN_MAX_AGE) o quedó inutilizable, en vez de dejarla abierta indefinidamente.
    try:
        remote_list, truncated = _fetch_smartcards_for_subscriber(session_id, subscriber_code, limit)
        stats = _reconcile_subscriber_smartcards(subscriber_code, remote_list, truncated=truncated)
        stats["subscriber_code"] = subscriber_code
        return stats
    finally:
        close_old_connections()


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
        # Abonados donde la paginación se cortó por el tope de páginas antes
        # de ver su catálogo completo de smartcards -- en esos casos no se
        # borró nada localmente para ese abonado (ver _reconcile_subscriber_
        # smartcards). Si este número sube mucho, subir
        # PANACCESS_SMARTCARD_SUBSCRIBER_SYNC_MAX_PAGES.
        "subscribers_truncated": 0,
    }
    concurrency = PanaccessConfig.SMARTCARD_SUBSCRIBER_CONCURRENCY

    def _merge_stats(stats: dict) -> None:
        totals["updated"] += stats.get("updated", 0)
        totals["created"] += stats.get("created", 0)
        totals["deleted"] += stats.get("deleted", 0)
        totals["remote_count"] += stats.get("remote_count", 0)
        totals["subscribers_processed"] += 1
        if stats.get("truncated"):
            totals["subscribers_truncated"] += 1

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
