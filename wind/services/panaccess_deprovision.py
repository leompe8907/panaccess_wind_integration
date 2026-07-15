"""
Desaprovisionamiento en PanAccess al cerrar cuenta.

Orden confirmado con el equipo de servicios de PanAccess contra el WSDL
oficial de operador (v4.3,
https://cv01.panaccess.com/?requestMode=wsdl&v=4.3&r=operator):

  1) getOrdersOfSubscriber            -> orderId/productId reales por smartcard
  2) RemoveLicenseBlockFromSubscriber -> licencias de streaming a nivel suscriptor
  3) DisableOrderOfSubscriber         -> ordenes sin smartcard asociada (sn
                                          nulo -- p.ej. producto de registro
                                          nunca asignado a una tarjeta fisica)
  4) CleanSmartcards                  -> limpia TODAS las ordenes/productos de
                                          las smartcards del suscriptor
  5) RemoveSmartcardFromSubscriber    -> desvincula cada smartcard del suscriptor
  6) DeleteSubscriber                 -> borra el suscriptor (lanza
                                          subscriber_has_smartcards si el paso 5
                                          no se completo para alguna tarjeta)

Del grupo de operaciones que propuso el equipo de PanAccess para limpiar
productos/ordenes de las smartcards (RemoveProductFromSmartcards,
RemoveSmartcardFromOrder, CleanSmartcards), solo se dejo CleanSmartcards en el
flujo: se confirmo en una prueba real de punta a punta (backend + PanAccess)
que alcanza por si sola, asi que las otras dos no se incluyen para no hacer
llamadas de mas.

El resultado de "success" solo depende de que el suscriptor se haya borrado
(paso 6) y de que ninguna smartcard haya quedado sin desvincular (paso 5). Las
fallas en licencias/ordenes puntuales/limpieza de tarjetas quedan en
"warnings" y no bloquean el cierre.
"""
from __future__ import annotations

import logging
from typing import Any

from appConfig import PanaccessConfig
from wind.exceptions import PanAccessException
from wind.functions.getSubscriber import CallGetSubscriber, CallGetOrdersOfSubscriber
from wind.services import get_panaccess

logger = logging.getLogger(__name__)


def _normalize_smartcards(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if item]
    return [str(raw).strip()]


def _smartcard_array_params(smartcards: list[str]) -> dict[str, str]:
    """
    PanAccess espera los arreglos como parametros indexados
    (smartcards[0]=X&smartcards[1]=Y...) en el puente HTTP, no como JSON.
    Confirmado porque asi lo hace ya create_subscriber.py para
    addProductToSmartcards (que funciona en producción para el alta).
    """
    return {f"smartcards[{idx}]": str(sn) for idx, sn in enumerate(smartcards) if sn}


def _alternate_api_name(api_name: str) -> str:
    """
    Calcula la variante con/sin prefijo "cv" de una operacion PanAccess, para
    reintentar automaticamente si la forma configurada no tiene permiso.
    """
    if api_name.startswith("cv") and len(api_name) > 2 and api_name[2].isupper():
        return api_name[2].lower() + api_name[3:]
    return "cv" + api_name[0].upper() + api_name[1:]


def _extract_product_ids_from_orders(orders: list[dict]) -> list[str]:
    """
    Extrae productIds reales desde getOrdersOfSubscriber. Reemplaza el uso
    anterior de _extract_product_ids(row), que leia 'products'/'productEntries'
    a nivel de todo el suscriptor (no desglosado por tarjeta) -- bug ya
    señalado en la auditoría.
    """
    ids: list[str] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        pid = order.get("productId")
        if pid is not None and str(pid) not in ids:
            ids.append(str(pid))
    return ids


def deprovision_subscriber_in_panaccess(
    subscriber_code: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Cierre real en PanAccess: quita licencias, productos y smartcards, y
    finalmente borra el suscriptor (cvDeleteSubscriber).
    """
    panaccess = get_panaccess()
    row = CallGetSubscriber(subscriber_code=subscriber_code) or {}
    smartcards = _normalize_smartcards(row.get("smartcards"))
    orders = CallGetOrdersOfSubscriber(subscriber_code=subscriber_code, include_expired=True)

    registration_product = (PanaccessConfig.REGISTRATION_PRODUCT_ID or "").strip()
    product_ids = _extract_product_ids_from_orders(orders)
    if registration_product and registration_product not in product_ids:
        product_ids.append(registration_product)

    plan = {
        "subscriber_code": subscriber_code,
        "smartcards": smartcards,
        "orders": [
            {
                "orderId": o.get("orderId"),
                "productId": o.get("productId"),
                "sn": o.get("sn"),
                "disabled": o.get("disabled"),
            }
            for o in orders
        ],
        "product_ids": product_ids,
    }

    if dry_run:
        return {"dry_run": True, "plan": plan}

    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []

    def _try_call(api_name: str, params: dict) -> tuple[dict | None, str | None]:
        try:
            return panaccess.call(api_name, params), None
        except PanAccessException as exc:
            return None, str(exc)

    def _run(step_name: str, api_name: str, params: dict, *, critical: bool) -> dict | None:
        # El resto del proyecto no usa el prefijo "cv" en casi ninguna
        # llamada (getSubscriber, addSubscriber, addLicenseBlockToSubscriber,
        # etc.), y se confirmo en pruebas reales que la cuenta de servicio no
        # tiene permiso para varias operaciones "cv...". Se intenta primero
        # con el nombre configurado y, si falla, se reintenta automaticamente
        # con la variante alterna (con/sin "cv") antes de darse por vencido --
        # igual al patron ya usado en create_subscriber.py para
        # validateContactOfSubscriber/cvValidateContactOfSubscriber.
        response, error = _try_call(api_name, params)
        used_name = api_name
        ok = bool(response and response.get("success"))

        if not ok:
            alt_name = _alternate_api_name(api_name)
            alt_response, alt_error = _try_call(alt_name, params)
            if alt_response and alt_response.get("success"):
                response, error, used_name, ok = alt_response, None, alt_name, True

        steps.append({
            "step": step_name,
            "api": used_name,
            "params": params,
            "success": ok,
            "response": response,
            "error": error,
        })
        if not ok:
            msg_source = error or (response.get("errorMessage") if response else None) or "error desconocido"
            (errors if critical else warnings).append(f"{step_name} ({api_name}): {msg_source}")
        return response

    # 1) Licencias de streaming a nivel suscriptor. Best-effort: la
    #    documentación no deja claro cuándo "ya no quedan" bloques por remover
    #    (los @throws documentados son genéricos), así que se intenta una vez
    #    y no bloquea el resto del cierre si falla.
    _run(
        "remove_license_block",
        PanaccessConfig.REMOVE_LICENSE_BLOCK_API,
        {"code": subscriber_code},
        critical=False,
    )

    # 2) Ordenes sin smartcard asociada (sn nulo -- p.ej. productos de
    #    registro nunca asignados a una tarjeta fisica). Ninguna llamada
    #    basada en smartcard aplica aqui; se desactiva la orden directamente
    #    por orderId. No-op documentado si ya esta deshabilitada/expirada.
    for order in orders:
        sn = order.get("sn")
        order_id = order.get("orderId")
        if sn or order_id is None or order.get("disabled"):
            continue
        params = {"subscriberCode": subscriber_code, "orderId": order_id}
        _run(
            f"disable_order_{order_id}",
            PanaccessConfig.DISABLE_ORDER_API,
            params,
            critical=False,
        )

    # 3) Limpia todas las ordenes/productos de las smartcards del suscriptor.
    #    Confirmado en prueba real de punta a punta que alcanza por si sola.
    if smartcards:
        _run(
            "clean_smartcards",
            PanaccessConfig.CLEAN_SMARTCARDS_API,
            _smartcard_array_params(smartcards),
            critical=False,
        )

    # 4) Desvincular cada smartcard del suscriptor. Esto sí es crítico: si
    #    alguna tarjeta no se desvincula, deleteSubscriber va a fallar con
    #    subscriber_has_smartcards.
    smartcards_removed = 0
    for sn in smartcards:
        response = _run(
            f"remove_smartcard_{sn}",
            PanaccessConfig.REMOVE_SMARTCARD_API,
            {"smartcardId": sn},
            critical=True,
        )
        if response and response.get("success"):
            smartcards_removed += 1

    # 6) Borrar el suscriptor. Si PanAccess todavía ve smartcards asociadas
    #    (subscriber_has_smartcards), queda explícito en "errors" que algún
    #    paso anterior no terminó de desvincular todo.
    subscriber_deleted = False
    delete_response = _run(
        "delete_subscriber",
        PanaccessConfig.DELETE_SUBSCRIBER_API,
        {"code": subscriber_code},
        critical=True,
    )
    if delete_response and delete_response.get("success"):
        subscriber_deleted = True

    return {
        "dry_run": False,
        "subscriber_code": subscriber_code,
        "smartcards_processed": len(smartcards),
        "smartcards_removed": smartcards_removed,
        "subscriber_deleted": subscriber_deleted,
        "steps": steps,
        "warnings": warnings,
        "errors": errors,
        "success": subscriber_deleted and not errors,
    }
