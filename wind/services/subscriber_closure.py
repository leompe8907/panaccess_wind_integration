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
from wind.services.device_session_service import revoke_all_device_sessions_for_subscriber
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
    """
    Desactiva el/los `User` de Django vinculados a este abonado y corta
    cualquier sesión JWT que ya tuvieran activa (auditoría, sección
    17/21/22): `is_active=False` ya hace que `JWTAuthentication` rechace el
    access token en la siguiente request, pero sin invalidar también el
    token en sí (blacklist de refresh + corte de `iat`), un access token
    todavía vigente emitido *antes* de este cierre seguiría sirviendo hasta
    que expire por su cuenta si en algún momento el usuario se reactivara
    por error. Se itera (en vez de un `.update()` en bloque) porque
    `invalidate_active_sessions` necesita el objeto `User` real por cada
    uno.
    """
    from wind.models import SubscriberLoginInfo
    from django.db.models import Q
    from django.db.models.functions import Lower
    from wind.db_router import use_primary_for_reads

    # Estas lecturas ocurren justo después de escribir el tombstone de
    # cierre (mismo request, milisegundos de diferencia) -- forzar primaria
    # evita que un lag de réplica haga que esta función no encuentre el
    # email/subscriber recién escrito y deje sin desactivar/invalidar una
    # sesión que debía cortarse de inmediato (ver wind/db_router.py).
    with use_primary_for_reads():
        emails = set()
        for email in SubscriberEmailRegistry.objects.filter(subscriber_code=subscriber_code).values_list(
            "email", flat=True
        ):
            if email:
                emails.add(email.strip().lower())

        sub = ListOfSubscriber.objects.filter(code=subscriber_code).first()
        if sub and sub.emails:
            emails.add(sub.emails.strip().lower())

        usernames = {subscriber_code}
        login_info = SubscriberLoginInfo.objects.filter(subscriberCode=subscriber_code).first()
        if login_info:
            if login_info.login1:
                usernames.add(str(login_info.login1))
            if login_info.login2:
                usernames.add(login_info.login2)

        from wind.services.jwt_invalidation import invalidate_active_sessions

        user_qs = User.objects.annotate(email_lower=Lower("email")).filter(
            Q(email_lower__in=list(emails)) | Q(username__in=list(usernames))
        )

        updated = 0
        for user in user_qs:
            invalidate_active_sessions(user)
            if user.is_active:
                user.is_active = False
                user.save(update_fields=["is_active"])
                updated += 1
        return updated


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

        # Igual que el tombstone de arriba: se corta el acceso al portal
        # DE UNA VEZ, antes de llamar a PanAccess -- no solo si la
        # desaprovisión termina en éxito completo más abajo. Antes,
        # `_deactivate_portal_users` solo corría tras un cierre 100%
        # exitoso; si PanAccess fallaba o quedaba parcial, el `User` seguía
        # activo y CUALQUIER sesión ya logueada (JWT emitido antes de este
        # cierre) seguía entrando al dashboard con normalidad, aunque el
        # abonado ya estuviera en PENDING_CLOSURE localmente (auditoría,
        # sección 17/21/22 -- confirmado en la práctica por el cliente: el
        # perfil devolvía 404 "sin suscriptor vinculado" pero el dashboard
        # seguía cargando, señal de que la sesión seguía autenticando bien).
        _deactivate_portal_users(subscriber_code)

        # Auditoría (segunda ronda): _revoke_udid_requests y la revocación
        # de DeviceSession (Fase 3/4) vivían más abajo, condicionadas a que
        # la desaprovisión en PanAccess terminara en éxito completo -- el
        # mismo hueco que la sección 22 ya había cerrado para
        # _deactivate_portal_users/JWT. Un cierre que queda PARTIAL (ver
        # sección 11: es un estado normal, no raro) dejaba pareos UDID y
        # dispositivos vinculados activos indefinidamente, sin aviso. Se
        # mueven acá, junto al resto del corte de acceso inmediato --
        # revocar dos veces (acá y si el flujo llega al final) sería
        # inofensivo (son operaciones idempotentes), pero se guardan los
        # conteos acá y NO se repite la llamada más abajo, para no
        # confundir logs con revocaciones duplicadas.
        # Ninguna de las dos debe poder tumbar el cierre completo si falla
        # -- son un efecto colateral de seguridad, no el objetivo principal
        # de este request.
        try:
            udid_revoked_count = _revoke_udid_requests(subscriber_code)
        except Exception:
            logger.exception("Error revocando UDIDAuthRequest de %s durante cierre de cuenta", subscriber_code)
            udid_revoked_count = None
        try:
            device_sessions_revoked_count = revoke_all_device_sessions_for_subscriber(
                subscriber_code, reason="account_closed"
            )
        except Exception:
            logger.exception("Error revocando DeviceSession de %s durante cierre de cuenta", subscriber_code)
            device_sessions_revoked_count = None
    else:
        udid_revoked_count = None
        device_sessions_revoked_count = None

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
            local_result={
                "skipped": "panaccess_partial_failure",
                # Aunque PanAccess quedó parcial, el acceso local ya se
                # cortó de una vez arriba (users/udid/device_sessions) --
                # se deja constancia acá para que quede trazado incluso en
                # un cierre que queda PARTIAL.
                "udid_revoked": udid_revoked_count,
                "device_sessions_revoked": device_sessions_revoked_count,
            },
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
    # udid_revoked/device_sessions_revoked ya se ejecutaron arriba (bloque
    # de corte de acceso inmediato, antes de llamar a PanAccess) -- se
    # reportan los mismos conteos acá para el log de éxito completo, sin
    # volver a revocar (idempotente de todos modos, pero evita duplicar la
    # notificación WebSocket de revocación a dispositivos ya conectados).
    local_result["udid_revoked"] = udid_revoked_count
    local_result["device_sessions_revoked"] = device_sessions_revoked_count

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
