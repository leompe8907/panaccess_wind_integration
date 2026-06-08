"""
Funciones para obtener y sincronizar información de login de suscriptores desde PanAccess.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import transaction

from appConfig import PanaccessConfig
from wind.models import SubscriberLoginInfo, ListOfSubscriber
from wind.utils.encryption import encrypt_value

from wind.services import get_panaccess
from wind.exceptions import PanAccessException

logger = logging.getLogger(__name__)

_LIST_LOGIN_API_CANDIDATES = (
    "getListOfSubscriberLoginInfo",
    "getListOfSubscriberLoginInfos",
    "getSubscriberLoginInfoList",
)

_ENTRY_KEYS = (
    "loginInfoEntries",
    "subscriberLoginInfoEntries",
    "subscriberLoginEntries",
    "entries",
    "rows",
)

_resolved_list_login_api: str | None | bool = False


def DataBaseEmpty():
    return not SubscriberLoginInfo.objects.exists()


def _login_info_from_answer(answer: dict, subscriber_code: str | None = None) -> dict | None:
    code = subscriber_code or answer.get("subscriberCode") or answer.get("code")
    if not code or not str(code).strip():
        return None
    code = str(code).strip()
    password_raw = answer.get("password")
    return {
        "subscriberCode": code,
        "login1": answer.get("login1"),
        "login2": answer.get("login2"),
        "additionalLogins": answer.get("additionalLogins"),
        "password_hash": encrypt_value(password_raw) if password_raw else None,
        "licenses": answer.get("licenses"),
    }


def _extract_login_entries(answer) -> list:
    if not isinstance(answer, dict):
        return []
    for key in _ENTRY_KEYS:
        entries = answer.get(key)
        if entries:
            return entries
    return []


def CallGetSubscriberLoginInfo(session_id=None, subscriber_code=None):
    if not subscriber_code:
        raise ValueError("subscriber_code es requerido")

    panaccess = get_panaccess()
    response = panaccess.call(
        "getSubscriberLoginInfo",
        {"subscriberCode": subscriber_code},
    )

    if response.get("success"):
        answer = response.get("answer", {})
        if isinstance(answer, dict):
            answer["subscriberCode"] = subscriber_code
        return answer

    error_message = response.get("errorMessage", "Error desconocido al obtener login info")
    raise PanAccessException(error_message)


def _resolve_list_login_api() -> str | None:
    global _resolved_list_login_api
    if _resolved_list_login_api is not False:
        return _resolved_list_login_api

    if not PanaccessConfig.LOGIN_INFO_TRY_LIST_API:
        _resolved_list_login_api = None
        return None

    panaccess = get_panaccess()
    for api_name in _LIST_LOGIN_API_CANDIDATES:
        try:
            response = panaccess.call(api_name, {"offset": 0, "limit": 1})
            if response.get("success"):
                logger.info("API listada de login info: %s", api_name)
                _resolved_list_login_api = api_name
                return api_name
        except PanAccessException as exc:
            logger.debug("API %s no disponible: %s", api_name, exc)
        except Exception as exc:
            logger.debug("Probe %s falló: %s", api_name, exc)

    logger.info("Sin API listada de login info; se usará fetch paralelo por subscriberCode")
    _resolved_list_login_api = None
    return None


def CallListSubscriberLoginInfo(session_id=None, offset=0, limit=100):
    api_name = _resolve_list_login_api()
    if not api_name:
        raise PanAccessException("No hay API listada de login info configurada en PanAccess")

    panaccess = get_panaccess()
    parameters = {
        "offset": offset,
        "limit": limit,
        "orderDir": "DESC",
        "orderBy": "subscriberCode",
    }
    response = panaccess.call(api_name, parameters)

    if response.get("success"):
        return response.get("answer", {})

    error_message = response.get("errorMessage", "Error desconocido al listar login info")
    raise PanAccessException(error_message)


def bulk_upsert_login_records(records: list[dict], *, chunk_size: int = 200) -> dict:
    by_code: dict[str, dict] = {}
    for raw in records:
        if not raw or not raw.get("subscriberCode"):
            continue
        code = str(raw["subscriberCode"]).strip()
        if code:
            by_code[code] = raw

    if not by_code:
        return {"created": 0, "updated": 0, "total": 0}

    codes = list(by_code.keys())
    existing = {
        obj.subscriberCode: obj
        for obj in SubscriberLoginInfo.objects.filter(subscriberCode__in=codes)
        if obj.subscriberCode
    }

    update_fields = [
        "login1",
        "login2",
        "additionalLogins",
        "password_hash",
        "licenses",
    ]
    to_create: list[SubscriberLoginInfo] = []
    to_update: list[SubscriberLoginInfo] = []

    for code, data in by_code.items():
        if code in existing:
            obj = existing[code]
            obj.login1 = data.get("login1")
            obj.login2 = data.get("login2")
            obj.additionalLogins = data.get("additionalLogins")
            obj.password_hash = data.get("password_hash")
            obj.licenses = data.get("licenses")
            to_update.append(obj)
        else:
            to_create.append(SubscriberLoginInfo(**data))

    created = 0
    updated = 0

    with transaction.atomic():
        if to_create:
            for i in range(0, len(to_create), chunk_size):
                batch = to_create[i : i + chunk_size]
                SubscriberLoginInfo.objects.bulk_create(batch, ignore_conflicts=True)
            created = len(to_create)

        if to_update:
            for i in range(0, len(to_update), chunk_size):
                batch = to_update[i : i + chunk_size]
                SubscriberLoginInfo.objects.bulk_update(batch, update_fields)
            updated = len(to_update)

    return {
        "created": created,
        "updated": updated,
        "total": len(by_code),
    }


def fetch_login_info_via_list_api(session_id=None, limit: int = 200) -> dict:
    api_name = _resolve_list_login_api()
    if not api_name:
        return {"mode": "list_api", "skipped": True, "reason": "no_list_api"}

    limit = max(1, min(limit, 1000))
    offset = 0
    remote_count = None
    all_records: list[dict] = []

    while True:
        answer = CallListSubscriberLoginInfo(session_id, offset=offset, limit=limit)
        if remote_count is None:
            remote_count = int(answer.get("count") or 0)

        entries = _extract_login_entries(answer)
        if not entries:
            break

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            record = _login_info_from_answer(entry)
            if record:
                all_records.append(record)

        offset += limit
        if remote_count and len(all_records) >= remote_count:
            break
        if len(entries) < limit:
            break

    upsert = bulk_upsert_login_records(all_records)
    logger.info(
        "Login info vía %s: %s registros remotos, creados=%s actualizados=%s",
        api_name,
        len(all_records),
        upsert["created"],
        upsert["updated"],
    )
    return {
        "mode": "list_api",
        "api": api_name,
        "remote_count": len(all_records),
        "remote_api_count": remote_count,
        **upsert,
    }


def _fetch_login_info_one_code(subscriber_code: str) -> dict | None:
    try:
        answer = CallGetSubscriberLoginInfo(None, subscriber_code)
        return _login_info_from_answer(answer, subscriber_code)
    except PanAccessException as exc:
        logger.warning("Login info falló para %s: %s", subscriber_code, exc)
        return None
    except Exception as exc:
        logger.error("Error inesperado login info %s: %s", subscriber_code, exc)
        return None


def fetch_login_info_for_codes(codes: list[str]) -> dict:
    codes = [str(c).strip() for c in codes if c and str(c).strip()]
    if not codes:
        return {"mode": "parallel_codes", "total": 0, "success": 0, "errors": 0}

    workers = PanaccessConfig.LOGIN_INFO_CONCURRENCY
    records: list[dict] = []
    errors = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_fetch_login_info_one_code, c): c for c in codes}
        for future in as_completed(futures):
            record = future.result()
            if record:
                records.append(record)
            else:
                errors += 1

    upsert = bulk_upsert_login_records(records)
    return {
        "mode": "parallel_codes",
        "total": len(codes),
        "success": upsert["total"],
        "errors": errors,
        **upsert,
    }


def fetch_login_info_via_parallel(
    session_id=None,
    limit: int | None = None,
    *,
    page_limit: int = 200,
) -> dict:
    del session_id

    qs = ListOfSubscriber.objects.exclude(code__isnull=True).exclude(code="")
    if limit:
        qs = qs[:limit]

    codes = list(qs.values_list("code", flat=True))
    total = len(codes)
    if not total:
        return {
            "mode": "parallel",
            "total": 0,
            "success": 0,
            "errors": 0,
            "skipped": 0,
        }

    workers = PanaccessConfig.LOGIN_INFO_CONCURRENCY
    logger.info("Login info paralelo: %s suscriptores, %s workers", total, workers)

    records: list[dict] = []
    errors = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_fetch_login_info_one_code, code): code for code in codes}
        done = 0
        for future in as_completed(futures):
            done += 1
            record = future.result()
            if record:
                records.append(record)
            else:
                errors += 1
            if done % 200 == 0:
                logger.info("Login info paralelo: %s/%s", done, total)

    chunk_size = PanaccessConfig.LOGIN_INFO_DB_CHUNK
    upsert = bulk_upsert_login_records(records, chunk_size=chunk_size)

    return {
        "mode": "parallel",
        "total": total,
        "success": upsert["total"],
        "errors": errors,
        "skipped": 0,
        "workers": workers,
        "page_limit": page_limit,
        **upsert,
    }


def store_login_info_in_chunks(login_info_list, chunk_size=100):
    result = bulk_upsert_login_records(login_info_list, chunk_size=chunk_size)
    return result.get("created", 0) + result.get("updated", 0)


def fetch_login_info_for_subscriber(session_id=None, subscriber_code=None):
    record = _fetch_login_info_one_code(subscriber_code)
    if not record:
        return False
    bulk_upsert_login_records([record])
    return True


def fetch_all_subscribers_login_info(session_id=None, limit=None):
    page_limit = PanaccessConfig.LOGIN_INFO_PAGE_LIMIT

    if _resolve_list_login_api():
        try:
            result = fetch_login_info_via_list_api(session_id, limit=page_limit)
            if not result.get("skipped"):
                return {
                    "total": result.get("remote_count", 0),
                    "success": result.get("total", 0),
                    "errors": 0,
                    "skipped": 0,
                    **result,
                }
        except PanAccessException as exc:
            logger.warning("List API de login info falló, usando modo paralelo: %s", exc)

    parallel = fetch_login_info_via_parallel(session_id, limit=limit, page_limit=page_limit)
    return {
        "total": parallel.get("total", 0),
        "success": parallel.get("success", 0),
        "errors": parallel.get("errors", 0),
        "skipped": parallel.get("skipped", 0),
        **parallel,
    }


def cleanup_login_info_not_in_remote(remote_subscriber_codes):
    if not remote_subscriber_codes:
        return 0
    qs = SubscriberLoginInfo.objects.exclude(subscriberCode__in=remote_subscriber_codes)
    qs = qs.exclude(subscriberCode__isnull=True).exclude(subscriberCode="")
    deleted = qs.delete()[0]
    if deleted:
        logger.info("Login info eliminada (ausente en PanAccess): %s registros", deleted)
    return deleted


def cleanup_login_info_orphans():
    local_codes = set(
        ListOfSubscriber.objects.exclude(code__isnull=True)
        .exclude(code="")
        .values_list("code", flat=True)
    )
    deleted = SubscriberLoginInfo.objects.exclude(subscriberCode__in=local_codes).delete()[0]
    return deleted


def sync_subscribers_login_info(session_id=None, limit=None):
    logger.info("Iniciando sincronización de login info de suscriptores")
    result = fetch_all_subscribers_login_info(session_id, limit)
    logger.info(
        "Sincronización login info completada: modo=%s éxitos=%s errores=%s",
        result.get("mode"),
        result.get("success"),
        result.get("errors"),
    )
    return result
