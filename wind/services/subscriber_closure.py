"""
Cierre de cuenta de abonado (desaprovisionar PanAccess + tombstone local).
"""
from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone

from wind.models import (
    ListOfSubscriber,
    SubscriberClosureLog,
    SubscriberDocumentRegistry,
    SubscriberEmailRegistry,
    UDIDAuthRequest,
)
from wind.services.panaccess_deprovision import deprovision_subscriber_in_panaccess
from wind.functions.getSubscriber import delete_subscriber_operational_data

logger = logging.getLogger(__name__)
User = get_user_model()


def _mark_registry_closed(subscriber_code: str, closed_at) -> dict[str, int]:
    email_updated = SubscriberEmailRegistry.objects.filter(
        subscriber_code=subscriber_code,
    ).update(
        account_closed_at=closed_at,
        closed_subscriber_code=subscriber_code,
        eligible_for_trial=False,
    )
    doc_updated = SubscriberDocumentRegistry.objects.filter(
        subscriber_code=subscriber_code,
    ).update(
        account_closed_at=closed_at,
        closed_subscriber_code=subscriber_code,
        eligible_for_trial=False,
    )
    return {"email_registry": email_updated, "document_registry": doc_updated}


def _deactivate_portal_users(subscriber_code: str) -> int:
    emails = list(
        SubscriberEmailRegistry.objects.filter(subscriber_code=subscriber_code).values_list(
            "email", flat=True
        )
    )
    if not emails:
        return 0
    return User.objects.filter(email__in=emails, is_active=True).update(is_active=False)


def _revoke_udid_requests(subscriber_code: str) -> int:
    now = timezone.now()
    return UDIDAuthRequest.objects.filter(
        subscriber_code=subscriber_code,
        status__in=["pending", "validated"],
    ).update(status="revoked", revoked_at=now, revoked_reason="account_closed")


def close_subscriber_account(
    subscriber_code: str,
    *,
    reason: str = "",
    requested_by=None,
    dry_run: bool = False,
    skip_panaccess: bool = False,
) -> dict[str, Any]:
    """
    Cierra la cuenta: PanAccess (productos -> smartcards) + local (sin borrar registry).
    """
    subscriber_code = (subscriber_code or "").strip()
    if not subscriber_code:
        return {"success": False, "message": "subscriber_code es requerido"}

    subscriber = ListOfSubscriber.objects.filter(code=subscriber_code).first()
    if subscriber and subscriber.status == ListOfSubscriber.STATUS_CLOSED and not dry_run:
        return {
            "success": True,
            "already_closed": True,
            "subscriber_code": subscriber_code,
            "message": "La cuenta ya estaba cerrada.",
        }

    if not dry_run:
        # Tombstone de entrada, ANTES de llamar a PanAccess: protege la fila
        # local durante todo el tiempo que tarde la desaprovisión (que puede
        # ser varios pasos/segundos), incluso si el suscriptor nunca se
        # habia sincronizado localmente antes. Sin esto, un
        # periodic_sync_pipeline_task/full_sync_task que corriera justo en
        # esa ventana podia insertar/refrescar la fila como "active" con
        # datos de PanAccess mientras el cierre real todavia estaba en
        # curso (_is_closure_tombstone solo protege status
        # CLOSED/PENDING_CLOSURE, y antes de este cambio esa marca no
        # existia hasta el final del proceso cuando no habia fila previa).
        if subscriber:
            if subscriber.status != ListOfSubscriber.STATUS_PENDING_CLOSURE:
                subscriber.status = ListOfSubscriber.STATUS_PENDING_CLOSURE
                subscriber.save(update_fields=["status"])
        else:
            subscriber, _ = ListOfSubscriber.objects.update_or_create(
                code=subscriber_code,
                defaults={
                    "id": subscriber_code,
                    "status": ListOfSubscriber.STATUS_PENDING_CLOSURE,
                },
            )

    panaccess_result: dict[str, Any] = {"skipped": skip_panaccess}
    if not skip_panaccess:
        panaccess_result = deprovision_subscriber_in_panaccess(
            subscriber_code,
            dry_run=dry_run,
        )

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "subscriber_code": subscriber_code,
            "panaccess": panaccess_result,
            "local_plan": {
                "status": ListOfSubscriber.STATUS_CLOSED,
                "preserve_registry": True,
                "operational_cleanup": True,
            },
        }

    closed_at = timezone.now()
    local_result: dict[str, Any] = {}

    if not skip_panaccess and not panaccess_result.get("success"):
        # Ya quedó en PENDING_CLOSURE arriba (antes de la llamada a
        # PanAccess); no hace falta volver a marcarlo aquí.
        log = SubscriberClosureLog.objects.create(
            subscriber_code=subscriber_code,
            requested_by=requested_by,
            reason=reason,
            dry_run=False,
            panaccess_result=panaccess_result,
            local_result={"skipped": "panaccess_partial_failure"},
            status=SubscriberClosureLog.STATUS_PARTIAL,
        )
        return {
            "success": False,
            "subscriber_code": subscriber_code,
            "panaccess": panaccess_result,
            "closure_log_id": log.id,
            "message": "Cierre parcial en PanAccess; reintente o revise logs.",
        }

    local_result["operational_deleted"] = delete_subscriber_operational_data(
        [subscriber_code],
        preserve_registry=True,
    )

    # A esta altura "subscriber" siempre existe: si no habia fila previa, el
    # tombstone de entrada (arriba) ya la creo con update_or_create.
    subscriber.smartcards = []
    subscriber.status = ListOfSubscriber.STATUS_CLOSED
    subscriber.closed_at = closed_at
    subscriber.closed_reason = reason or ""
    subscriber.save(update_fields=["smartcards", "status", "closed_at", "closed_reason"])

    local_result["registry"] = _mark_registry_closed(subscriber_code, closed_at)
    local_result["users_deactivated"] = _deactivate_portal_users(subscriber_code)
    local_result["udid_revoked"] = _revoke_udid_requests(subscriber_code)

    log_status = SubscriberClosureLog.STATUS_COMPLETED
    closure_log = SubscriberClosureLog.objects.create(
        subscriber_code=subscriber_code,
        requested_by=requested_by,
        reason=reason,
        dry_run=False,
        panaccess_result=panaccess_result,
        local_result=local_result,
        status=log_status,
    )

    return {
        "success": True,
        "subscriber_code": subscriber_code,
        "panaccess": panaccess_result,
        "local": local_result,
        "closure_log_id": closure_log.id,
        "re_registration": "allowed_without_trial",
        "message": "Cuenta cerrada correctamente.",
    }
