"""
Smartcards y productos del suscriptor vinculado al usuario del portal.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.utils.dateparse import parse_datetime
from django.utils.formats import date_format

from wind.functions.getSmartcard import (
    fetch_subscriber_smartcards_from_panaccess,
    normalize_smartcard_row,
)
from wind.functions.getSubscriber import CallGetSubscriber, extract_first_email
from wind.models import (
    ListOfProducts,
    ListOfSmartcards,
    ListOfSubscriber,
    SubscriberEmailRegistry,
    SubscriberLoginInfo,
)

User = get_user_model()
logger = logging.getLogger(__name__)

_SMARTCARD_MODEL_FIELDS = {f.name for f in ListOfSmartcards._meta.get_fields()}
_SUBSCRIBER_MODEL_FIELDS = {f.name for f in ListOfSubscriber._meta.get_fields()}


def _parse_panaccess_datetime(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value
    if isinstance(value, str):
        try:
            from dateutil import parser as date_parser
            return date_parser.parse(value)
        except Exception:
            return parse_datetime(value)
    return None


def _format_datetime(value) -> str | None:
    if not value:
        return None
    try:
        return date_format(value, "SHORT_DATETIME_FORMAT")
    except Exception:
        return str(value)


def _format_contact_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if v]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _format_address(address: dict | None) -> str | None:
    if not address or not isinstance(address, dict):
        return None
    parts = [
        address.get("street"),
        address.get("city"),
        address.get("zip"),
        address.get("country"),
    ]
    text = ", ".join(p for p in parts if p)
    return text or None


def _upsert_subscriber_from_panaccess_row(row: dict) -> ListOfSubscriber | None:
    """Guarda o actualiza un suscriptor completo desde fila PanAccess."""
    code = row.get("subscriberCode") or row.get("code")
    if not code or not str(code).strip():
        return None

    code = str(code).strip()
    defaults = {
        "id": code,
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
        "phones": row.get("phones"),
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
        "created": _parse_panaccess_datetime(row.get("created")),
        "firstOrderTime": _parse_panaccess_datetime(row.get("firstOrderTime")),
        "lastExpiryTime": _parse_panaccess_datetime(row.get("lastExpiryTime")),
    }
    filtered = {k: v for k, v in defaults.items() if k in _SUBSCRIBER_MODEL_FIELDS}
    sub, _ = ListOfSubscriber.objects.update_or_create(code=code, defaults=filtered)
    return sub


def resolve_subscriber_code_for_user(user) -> str | None:
    """Obtiene el código PanAccess del usuario autenticado."""
    if not user or not user.is_authenticated:
        return None

    try:
        reg = SubscriberEmailRegistry.objects.get(email__iexact=user.email)
        if reg.subscriber_code:
            return reg.subscriber_code
    except SubscriberEmailRegistry.DoesNotExist:
        pass

    if user.email and user.email.endswith("@subscribers.wind.local"):
        code = user.email.split("@", 1)[0]
        if code:
            return code

    if user.username and str(user.username).isdigit():
        info = SubscriberLoginInfo.objects.filter(login1=int(user.username)).first()
        if info and info.subscriberCode:
            return info.subscriberCode

    sub = ListOfSubscriber.objects.filter(emails__iexact=user.email).first()
    if sub and sub.code:
        return sub.code

    return None


def _subscriber_smartcard_sns(subscriber: ListOfSubscriber | None) -> list[str]:
    if not subscriber or not subscriber.smartcards:
        return []
    raw = subscriber.smartcards
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if x]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def get_smartcards_for_subscriber(subscriber_code: str):
    """Smartcards del suscriptor (por código o SN en ListOfSubscriber.smartcards)."""
    subscriber = ListOfSubscriber.objects.filter(code=subscriber_code).first()
    sns = _subscriber_smartcard_sns(subscriber)

    qs = ListOfSmartcards.objects.filter(subscriberCode=subscriber_code)
    if sns:
        from django.db.models import Q
        qs = ListOfSmartcards.objects.filter(
            Q(subscriberCode=subscriber_code) | Q(sn__in=sns)
        ).distinct()
    return qs.order_by("sn")


def _normalize_product_ids(products_field) -> list[int]:
    if products_field is None:
        return []
    if isinstance(products_field, int):
        return [products_field]
    if isinstance(products_field, list):
        ids: list[int] = []
        for item in products_field:
            if isinstance(item, int):
                ids.append(item)
            elif isinstance(item, str) and item.isdigit():
                ids.append(int(item))
            elif isinstance(item, dict):
                pid = item.get("productId") or item.get("id")
                if pid is not None:
                    ids.append(int(pid))
        return ids
    return []


def _resolve_smartcard_products(products_field) -> list[dict]:
    if products_field is None:
        return []

    if isinstance(products_field, list):
        numeric_ids = _normalize_product_ids(products_field)
        if numeric_ids:
            return _enrich_products_by_ids(numeric_ids)

        items: list[dict] = []
        for entry in products_field:
            if isinstance(entry, str) and entry.strip():
                items.append(
                    {
                        "productId": None,
                        "name": entry.strip(),
                        "description": None,
                        "packages": None,
                        "optionalPackages": None,
                    }
                )
            elif isinstance(entry, dict):
                pid = entry.get("productId") or entry.get("id")
                name = entry.get("name") or entry.get("productName")
                items.append(
                    {
                        "productId": int(pid) if pid is not None else None,
                        "name": name,
                        "description": entry.get("description"),
                        "packages": entry.get("packages"),
                        "optionalPackages": entry.get("optionalPackages"),
                    }
                )
        return items

    return []


def _enrich_products_by_ids(product_ids: list[int]) -> list[dict]:
    if not product_ids:
        return []

    catalog = {
        p.productId: p
        for p in ListOfProducts.objects.filter(productId__in=product_ids, deleted=False)
    }
    results = []
    for pid in product_ids:
        prod = catalog.get(pid)
        if prod:
            results.append(
                {
                    "productId": prod.productId,
                    "name": prod.name,
                    "description": prod.description,
                    "packages": prod.packages,
                    "optionalPackages": prod.optionalPackages,
                }
            )
        else:
            results.append(
                {
                    "productId": pid,
                    "name": None,
                    "description": None,
                    "packages": None,
                    "optionalPackages": None,
                }
            )
    return results


def _enrich_products_by_ids_from_catalog(product_ids: list[int], catalog: dict[int, ListOfProducts]) -> list[dict]:
    if not product_ids:
        return []
    results: list[dict] = []
    for pid in product_ids:
        prod = catalog.get(pid)
        if prod:
            results.append(
                {
                    "productId": prod.productId,
                    "name": prod.name,
                    "description": prod.description,
                    "packages": prod.packages,
                    "optionalPackages": prod.optionalPackages,
                }
            )
        else:
            results.append(
                {
                    "productId": pid,
                    "name": None,
                    "description": None,
                    "packages": None,
                    "optionalPackages": None,
                }
            )
    return results


def _upsert_smartcard_entry(entry: dict) -> None:
    sn = entry.get("sn")
    if not sn:
        return
    filtered = normalize_smartcard_row(entry)
    if not filtered.get("sn"):
        return
    ListOfSmartcards.objects.update_or_create(sn=filtered["sn"], defaults=filtered)


def _sync_subscriber_row_from_panaccess(subscriber_code: str) -> ListOfSubscriber | None:
    code = str(subscriber_code).strip() if subscriber_code else ""
    if not code:
        return None

    try:
        row = CallGetSubscriber(subscriber_code=code)
        if row:
            return _upsert_subscriber_from_panaccess_row(row)
    except Exception as exc:
        logger.warning(
            "No se pudo sincronizar suscriptor %s desde PanAccess (getSubscriber): %s",
            code,
            exc,
        )

    return ListOfSubscriber.objects.filter(code=code).first()


def _serialize_subscriber_detail(sub: ListOfSubscriber) -> dict:
    login_info = SubscriberLoginInfo.objects.filter(subscriberCode=sub.code).first()
    phones = _format_contact_list(sub.phones)
    mobiles = _format_contact_list(sub.mobiles)

    return {
        "code": sub.code,
        "firstName": sub.firstName,
        "lastName": sub.lastName,
        "fullName": " ".join(filter(None, [sub.firstName, sub.lastName])) or None,
        "emails": sub.emails,
        "phones": phones,
        "mobiles": mobiles,
        "countryCode": sub.countryCode,
        "regionId": sub.regionId,
        "supervisor": sub.supervisor,
        "uniqueLogin": sub.uniqueLogin,
        "login1": login_info.login1 if login_info else sub.uniqueLogin,
        "login2": login_info.login2 if login_info else None,
        "smartcard_sns": _subscriber_smartcard_sns(sub),
        "smartcards_count": len(_subscriber_smartcard_sns(sub)),
        "created": _format_datetime(sub.created),
        "firstOrderTime": _format_datetime(sub.firstOrderTime),
        "lastExpiryTime": _format_datetime(sub.lastExpiryTime),
        "newsletterAccepted": sub.newsletterAccepted,
        "comment": sub.comment,
        "address": _format_address(sub.address1),
        "caf": sub.caf,
    }


def get_subscriber_record(
    subscriber_code: str, *, refresh_if_missing: bool = True
) -> ListOfSubscriber | None:
    sub = ListOfSubscriber.objects.filter(code=subscriber_code).first()
    needs_refresh = not sub or (not sub.firstName and not sub.lastName and not sub.emails)
    if refresh_if_missing and needs_refresh:
        sub = _sync_subscriber_row_from_panaccess(subscriber_code) or sub
    return sub


def build_subscriber_detail_payload(
    subscriber_code: str, *, refresh_if_missing: bool = True
) -> dict | None:
    sub = get_subscriber_record(subscriber_code, refresh_if_missing=refresh_if_missing)
    if not sub:
        return None
    return _serialize_subscriber_detail(sub)


def refresh_smartcards_from_panaccess(
    subscriber_code: str,
    target_sns: list[str] | None = None,
    *,
    profile_mode: bool = True,
) -> int:
    result = fetch_subscriber_smartcards_from_panaccess(
        subscriber_code,
        target_sns,
        profile_mode=profile_mode,
    )
    saved = 0
    for entry in result.get("entries") or []:
        if isinstance(entry, dict):
            _upsert_smartcard_entry(entry)
            saved += 1

    if saved:
        logger.info(
            "Smartcards perfil/abonado %s: %s guardadas (SN objetivo=%s)",
            subscriber_code,
            saved,
            result.get("target_sns"),
        )
    return saved


def build_subscriber_products_payload(subscriber_code: str, *, refresh_if_empty: bool = True) -> dict:
    subscriber = get_subscriber_record(subscriber_code, refresh_if_missing=refresh_if_empty)
    smartcards_qs = get_smartcards_for_subscriber(subscriber_code)

    if refresh_if_empty and not smartcards_qs.exists():
        subscriber = _sync_subscriber_row_from_panaccess(subscriber_code) or subscriber
        sns = _subscriber_smartcard_sns(subscriber)
        refresh_smartcards_from_panaccess(
            subscriber_code,
            target_sns=sns,
            profile_mode=True,
        )
        smartcards_qs = get_smartcards_for_subscriber(subscriber_code)

    cards = list(smartcards_qs)
    all_product_ids: set[int] = set()
    for card in cards:
        all_product_ids.update(_normalize_product_ids(card.products))

    catalog: dict[int, ListOfProducts] = {}
    if all_product_ids:
        catalog = {
            p.productId: p
            for p in ListOfProducts.objects.filter(productId__in=all_product_ids, deleted=False)
        }

    smartcards_payload: list[dict] = []
    for card in cards:
        product_ids = _normalize_product_ids(card.products)
        products = (
            _enrich_products_by_ids_from_catalog(product_ids, catalog)
            if product_ids
            else _resolve_smartcard_products(card.products)
        )
        smartcards_payload.append(
            {
                "sn": card.sn,
                "alias": card.alias,
                "subscriberCode": card.subscriberCode,
                "firstName": card.firstName,
                "lastName": card.lastName,
                "disabled": card.disabled,
                "blacklisted": card.blacklisted,
                "stbModel": card.stbModel,
                "packageNames": card.packageNames,
                "packages": card.packages,
                "productIds": product_ids,
                "products": products,
            }
        )

    known_sns = {c["sn"] for c in smartcards_payload if c["sn"]}
    for sn in _subscriber_smartcard_sns(subscriber):
        if sn not in known_sns:
            smartcards_payload.append(
                {
                    "sn": sn,
                    "alias": None,
                    "subscriberCode": subscriber_code,
                    "firstName": None,
                    "lastName": None,
                    "disabled": None,
                    "blacklisted": None,
                    "stbModel": None,
                    "packageNames": None,
                    "packages": None,
                    "productIds": [],
                    "products": [],
                    "pending_sync": True,
                }
            )

    subscriber_detail = (
        _serialize_subscriber_detail(subscriber) if subscriber else None
    )

    return {
        "subscriber_code": subscriber_code,
        "subscriber": subscriber_detail,
        "smartcards": smartcards_payload,
        "smartcards_count": len(smartcards_payload),
    }
