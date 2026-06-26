"""
Desaprovisionamiento en PanAccess al cerrar cuenta.

Orden acordado:
  1) Remover productos de cada smartcard
  2) Remover smartcards del suscriptor
"""
from __future__ import annotations

import logging
from typing import Any

from appConfig import PanaccessConfig
from wind.exceptions import PanAccessException
from wind.functions.getSubscriber import CallGetSubscriber
from wind.services import get_panaccess

logger = logging.getLogger(__name__)


def _normalize_smartcards(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if item]
    return [str(raw).strip()]


def _extract_product_ids(smartcard_row: dict) -> list[str]:
    """Extrae IDs de producto de la fila de smartcard si PanAccess los expone."""
    products = smartcard_row.get("products") or smartcard_row.get("productEntries") or []
    ids: list[str] = []
    if isinstance(products, list):
        for item in products:
            if isinstance(item, dict):
                pid = item.get("productId") or item.get("id")
                if pid is not None:
                    ids.append(str(pid))
            elif item is not None:
                ids.append(str(item))
    return ids


def deprovision_subscriber_in_panaccess(
    subscriber_code: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Quita productos de las tarjetas y luego desvincula tarjetas del suscriptor.
    """
    panaccess = get_panaccess()
    row = CallGetSubscriber(subscriber_code=subscriber_code) or {}
    smartcards = _normalize_smartcards(row.get("smartcards"))

    plan = {
        "subscriber_code": subscriber_code,
        "smartcards": smartcards,
        "remove_product_calls": [],
        "remove_smartcard_calls": [],
    }

    if dry_run:
        for sn in smartcards:
            plan["remove_product_calls"].append({"smartcard": sn, "note": "confirmar productIds en API"})
            plan["remove_smartcard_calls"].append(
                {"code": subscriber_code, "smartcard": sn, "api": PanaccessConfig.REMOVE_SMARTCARD_API}
            )
        return {"dry_run": True, "plan": plan}

    products_removed = 0
    smartcards_removed = 0
    errors: list[str] = []

    registration_product = (PanaccessConfig.REGISTRATION_PRODUCT_ID or "").strip()

    for sn in smartcards:
        product_ids = _extract_product_ids(row)
        if registration_product and registration_product not in product_ids:
            product_ids.append(registration_product)

        for product_id in product_ids or [registration_product]:
            if not product_id:
                continue
            params = {
                "code": subscriber_code,
                "smartcard": sn,
                "productId": product_id,
            }
            try:
                response = panaccess.call(PanaccessConfig.REMOVE_PRODUCT_API, params)
                if response.get("success"):
                    products_removed += 1
                    logger.info(
                        "[Deprovision] Producto %s removido de SC %s (subscriber=%s)",
                        product_id,
                        sn,
                        subscriber_code,
                    )
                else:
                    msg = response.get("errorMessage", "Error desconocido")
                    errors.append(f"remove_product {sn}/{product_id}: {msg}")
            except PanAccessException as exc:
                errors.append(f"remove_product {sn}/{product_id}: {exc}")

    for sn in smartcards:
        params = {
            "code": subscriber_code,
            "smartcard": sn,
        }
        try:
            response = panaccess.call(PanaccessConfig.REMOVE_SMARTCARD_API, params)
            if response.get("success"):
                smartcards_removed += 1
                logger.info(
                    "[Deprovision] Smartcard %s removida de subscriber %s",
                    sn,
                    subscriber_code,
                )
            else:
                msg = response.get("errorMessage", "Error desconocido")
                errors.append(f"remove_smartcard {sn}: {msg}")
        except PanAccessException as exc:
            errors.append(f"remove_smartcard {sn}: {exc}")

    return {
        "dry_run": False,
        "subscriber_code": subscriber_code,
        "smartcards_processed": len(smartcards),
        "products_removed": products_removed,
        "smartcards_removed": smartcards_removed,
        "errors": errors,
        "success": len(errors) == 0,
    }
