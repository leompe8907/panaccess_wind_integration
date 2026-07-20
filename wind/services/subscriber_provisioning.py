"""
Estado de aprovisionamiento parcial de suscriptores.

`addSubscriber` en PanAccess es solo el primer paso del alta real: todavía
faltan los contactos (email/teléfono), el license block y, si aplica, el
producto de prueba. Cualquiera de esos pasos puede fallar (falta de
licencias, hiccup transitorio de PanAccess, etc.) sin que el registro se
aborte -- antes eso dejaba al suscriptor "a medias" para siempre, sin
ninguna señal de que faltaba terminar de aprovisionarlo (auditoría:
"create_subscriber.py -- fallos parciales no abortan el registro").

Ahora cada intento de terminar el aprovisionamiento (síncrono en
`create_subscriber_view` vía `_create_subscriber_core`, o async en
`wind.tasks.finish_subscriber_provisioning_task`) guarda en
`ListOfSubscriber` qué pasos quedaron pendientes
(`provisioning_status`/`provisioning_pending_steps`), y
`wind.tasks.retry_partial_provisioning_task` (Celery, periódico) los
reintenta -- los mismos pasos (`addContactToSubscriber`,
`addLicenseBlockToSubscriber`, `addProductToSmartcards`) son idempotentes:
PanAccess los trata como "ya existe"/no-op si se repiten, así que
reintentarlos a ciegas es seguro (mismo razonamiento que ya usa
`finish_subscriber_provisioning_task` para sus propios reintentos).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

STEP_EMAIL_CONTACT = "email_contact"
STEP_PHONE_CONTACT = "phone_contact"
STEP_LICENSE_BLOCK = "license_block"
STEP_TRIAL_PRODUCT = "trial_product"


def compute_pending_steps(
    *,
    email_contact_ok: bool,
    phone_expected: bool,
    phone_contact_ok: bool,
    license_block_ok: bool,
    trial_expected: bool,
    trial_ok: bool,
) -> list[str]:
    """Determina qué pasos de aprovisionamiento quedaron pendientes."""
    pending = []
    if not email_contact_ok:
        pending.append(STEP_EMAIL_CONTACT)
    if phone_expected and not phone_contact_ok:
        pending.append(STEP_PHONE_CONTACT)
    if not license_block_ok:
        pending.append(STEP_LICENSE_BLOCK)
    if trial_expected and not trial_ok:
        pending.append(STEP_TRIAL_PRODUCT)
    return pending


def save_provisioning_result(subscriber_code: str, pending_steps: list[str]) -> None:
    """
    Persiste en ListOfSubscriber el resultado de un intento de terminar el
    aprovisionamiento. Si `pending_steps` viene vacío, marca 'complete' y
    limpia cualquier pendiente anterior -- así un reintento exitoso saca al
    suscriptor de la cola de `retry_partial_provisioning_task`.

    No lanza si la fila todavía no existe localmente (puede pasar si el
    guardado en BD también falló); solo lo deja registrado en el log, ya
    que sin fila local no hay dónde marcar el estado.
    """
    from wind.models import ListOfSubscriber

    new_status = (
        ListOfSubscriber.PROVISIONING_PARTIAL
        if pending_steps
        else ListOfSubscriber.PROVISIONING_COMPLETE
    )
    try:
        updated = ListOfSubscriber.objects.filter(code=subscriber_code).update(
            provisioning_status=new_status,
            provisioning_pending_steps=list(pending_steps) or None,
        )
        if not updated:
            logger.warning(
                "No se pudo guardar provisioning_status para %s "
                "(fila no encontrada en ListOfSubscriber)",
                subscriber_code,
            )
        elif pending_steps:
            logger.warning(
                "Suscriptor %s queda con aprovisionamiento parcial, "
                "pasos pendientes: %s",
                subscriber_code,
                pending_steps,
            )
        else:
            logger.info("Suscriptor %s: aprovisionamiento completo", subscriber_code)
    except Exception:
        logger.exception("Error guardando provisioning_status para %s", subscriber_code)


def attempt_pending_provisioning_steps(subscriber) -> dict:
    """
    Reintenta, para un ListOfSubscriber ya existente con
    provisioning_status=PARTIAL, únicamente los pasos que sigan marcados
    como pendientes en `provisioning_pending_steps`. Usada por
    `wind.tasks.retry_partial_provisioning_task`.

    No decide elegibilidad de trial desde cero con la lógica completa de
    registro (eso vive en `wind.services.subscriber_trial`) -- para el
    reintento periódico, "trial_product" solo se reintenta si
    `is_eligible_for_trial()` todavía lo permite en este momento (pudo
    dejar de aplicar, p. ej. si el suscriptor se cerró y reabrió con otro
    trial ya usado); el documento usado para esa verificación es el propio
    `code` del suscriptor cuando no es un código autogenerado (prefijo
    "AUTO"), replicando la convención de
    `wind.functions.create_subscriber.generate_unique_subscriber_code`.
    """
    from wind.exceptions import PanAccessException
    from wind.functions.create_subscriber import (
        _persist_subscriber_contacts_to_db,
        _resolve_email_contact_id,
        _validate_email_contact_of_subscriber,
    )
    from wind.functions.getSubscriber import CallGetSubscriber
    from wind.services import get_panaccess
    from wind.services.subscriber_trial import is_eligible_for_trial, mark_trial_granted, registration_trial_days

    pending = set(subscriber.provisioning_pending_steps or [])
    if not pending:
        return {"attempted": [], "still_pending": []}

    panaccess = get_panaccess()
    subscriber_code = subscriber.code
    email_normalized = (subscriber.emails or "").strip().lower()
    phone_normalized = subscriber.phones if isinstance(subscriber.phones, str) else None
    likely_document = None if subscriber_code.upper().startswith("AUTO") else subscriber_code

    still_pending = set(pending)

    if STEP_EMAIL_CONTACT in pending and email_normalized:
        try:
            add_resp = panaccess.call(
                "addContactToSubscriber",
                {"code": subscriber_code, "type": "email", "isBusiness": False, "contact": email_normalized},
            )
            if add_resp.get("success"):
                contact_id = _resolve_email_contact_id(panaccess, subscriber_code, add_resp, email_normalized)
                if contact_id is not None:
                    _validate_email_contact_of_subscriber(panaccess, subscriber_code, contact_id, email_normalized)
                still_pending.discard(STEP_EMAIL_CONTACT)
            else:
                logger.warning(
                    "[ProvisioningRetry] addContactToSubscriber (email) sigue fallando para %s: %s",
                    subscriber_code,
                    add_resp.get("errorMessage"),
                )
        except PanAccessException:
            logger.exception("[ProvisioningRetry] Error reintentando contacto email para %s", subscriber_code)

    if STEP_PHONE_CONTACT in pending and phone_normalized:
        try:
            phone_resp = panaccess.call(
                "addContactToSubscriber",
                {"code": subscriber_code, "type": "phone", "isBusiness": False, "contact": phone_normalized},
            )
            if phone_resp.get("success"):
                still_pending.discard(STEP_PHONE_CONTACT)
            else:
                logger.warning(
                    "[ProvisioningRetry] addContactToSubscriber (phone) sigue fallando para %s: %s",
                    subscriber_code,
                    phone_resp.get("errorMessage"),
                )
        except PanAccessException:
            logger.exception("[ProvisioningRetry] Error reintentando contacto phone para %s", subscriber_code)

    if STEP_EMAIL_CONTACT not in still_pending or STEP_PHONE_CONTACT not in still_pending:
        _persist_subscriber_contacts_to_db(
            subscriber_code,
            email=email_normalized if STEP_EMAIL_CONTACT not in still_pending else "",
            phone=phone_normalized if STEP_PHONE_CONTACT not in still_pending else "",
        )

    license_block_ok_now = STEP_LICENSE_BLOCK not in pending
    if STEP_LICENSE_BLOCK in pending:
        try:
            license_resp = panaccess.call("addLicenseBlockToSubscriber", {"code": subscriber_code})
            if license_resp.get("success"):
                license_block_ok_now = True
                still_pending.discard(STEP_LICENSE_BLOCK)
            else:
                logger.warning(
                    "[ProvisioningRetry] addLicenseBlockToSubscriber sigue fallando para %s: %s",
                    subscriber_code,
                    license_resp.get("errorMessage"),
                )
        except PanAccessException:
            logger.exception("[ProvisioningRetry] Error reintentando license block para %s", subscriber_code)

    if STEP_TRIAL_PRODUCT in pending and license_block_ok_now:
        trial_still_eligible = is_eligible_for_trial(email=email_normalized, document=likely_document)
        if not trial_still_eligible:
            logger.info(
                "[ProvisioningRetry] %s ya no es elegible para trial (probablemente ya otorgado/usado), "
                "se retira de pendientes sin reintentar",
                subscriber_code,
            )
            still_pending.discard(STEP_TRIAL_PRODUCT)
        else:
            try:
                found = CallGetSubscriber(subscriber_code=subscriber_code)
            except PanAccessException:
                found = None
            smartcards = (found or {}).get("smartcards")
            if smartcards:
                try:
                    from appConfig import PanaccessConfig
                    from datetime import timedelta
                    from django.utils import timezone

                    trial_days = registration_trial_days()
                    expiry_time = timezone.now() + timedelta(days=trial_days)
                    product_params = {
                        "productId": PanaccessConfig.REGISTRATION_PRODUCT_ID,
                        "hcId": PanaccessConfig.HCID,
                        "expiryTime": expiry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    for idx, sn in enumerate(smartcards):
                        if sn:
                            product_params[f"smartcards[{idx}]"] = str(sn)
                    product_resp = panaccess.call("addProductToSmartcards", product_params)
                    if product_resp.get("success"):
                        mark_trial_granted(
                            email=email_normalized,
                            document=likely_document,
                            subscriber_code=subscriber_code,
                            granted_at=timezone.now(),
                        )
                        still_pending.discard(STEP_TRIAL_PRODUCT)
                    else:
                        logger.warning(
                            "[ProvisioningRetry] addProductToSmartcards sigue fallando para %s: %s",
                            subscriber_code,
                            product_resp.get("errorMessage"),
                        )
                except PanAccessException:
                    logger.exception(
                        "[ProvisioningRetry] Error reintentando producto de prueba para %s", subscriber_code
                    )
                except Exception:
                    logger.exception(
                        "[ProvisioningRetry] Error inesperado reintentando producto de prueba para %s",
                        subscriber_code,
                    )
            else:
                logger.info(
                    "[ProvisioningRetry] %s todavía no tiene smartcards asignadas, se reintenta más adelante",
                    subscriber_code,
                )

    attempted = list(pending)
    return {"attempted": attempted, "still_pending": sorted(still_pending)}
