import logging
import time

from celery import shared_task
from django.utils import timezone

from wind.functions.getSubscriber import (
    compare_and_update_all_subscribers,
    sync_subscribers,
)
from wind.functions.getProducts import sync_products
from wind.functions.getSmartcard import compare_and_update_all_smartcards, run_smartcard_sync_for_pipeline, sync_smartcards
from wind.functions.full_sync import run_full_sync
from wind.exceptions import (
    PanAccessConnectionError,
    PanAccessException,
    PanAccessRateLimitError,
    PanAccessSessionError,
    PanAccessTimeoutError,
)
from appConfig import CeleryConfig, RedisConfig

logger = logging.getLogger(__name__)


def _skipped_already_running(task_name: str) -> dict:
    logger.warning("[Celery] %s ya está ejecutándose, se omite", task_name)
    return {
        "success": False,
        "skipped": True,
        "message": "Task already running, skipped",
    }


def _skipped_during_full_sync(task_name: str) -> dict:
    logger.warning(
        "[Celery] %s omitida: full_sync correctivo en curso",
        task_name,
    )
    return {
        "success": False,
        "skipped": True,
        "message": "Deferred: full_sync in progress",
    }


@shared_task(
    bind=True,
    autoretry_for=(
        PanAccessConnectionError,
        PanAccessTimeoutError,
        PanAccessSessionError,
        PanAccessRateLimitError,
        ConnectionError,
    ),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def periodic_sync_pipeline_task(self, limit=None):
    """
    Orquestador periódico: sync suscriptores y luego smartcards, en serie.

    Diseñado para un worker dedicado (-Q sync_pipeline -c 1).
    Se omite si full_sync está en curso.
    """
    if RedisConfig.is_full_sync_in_progress():
        return _skipped_during_full_sync("periodic_sync_pipeline_task")

    lock_key = "celery:lock:periodic_sync_pipeline_task"
    lock_timeout = CeleryConfig.PIPELINE_LOCK_TIMEOUT

    with RedisConfig.task_lock(lock_key, timeout=lock_timeout) as acquired:
        if not acquired:
            return _skipped_already_running("periodic_sync_pipeline_task")

        try:
            env_limit = CeleryConfig.SYNC_LIMIT
            limit = limit or env_limit or 200
            started = time.monotonic()

            logger.info(
                "[Celery] Pipeline sync — paso 1/2: sync_subscribers limit=%s",
                limit,
            )
            subscribers_result = sync_subscribers(session_id=None, limit=limit)

            logger.info(
                "[Celery] Pipeline sync — paso 2/2: smartcards (híbrido incremental) limit=%s",
                limit,
            )
            smartcards_result = run_smartcard_sync_for_pipeline(
                session_id=None, limit=limit
            )

            elapsed = round(time.monotonic() - started, 2)
            logger.info("[Celery] Pipeline sync completado en %ss", elapsed)

            return {
                "success": True,
                "limit": limit,
                "duration_seconds": elapsed,
                "subscribers": subscribers_result,
                "smartcards": smartcards_result,
            }
        except PanAccessException:
            logger.error("[Celery] Error PanAccess en periodic_sync_pipeline_task")
            raise
        except Exception:
            logger.exception(
                "[Celery] Error inesperado en periodic_sync_pipeline_task"
            )
            raise


@shared_task(
    bind=True,
    autoretry_for=(
        PanAccessConnectionError,
        PanAccessTimeoutError,
        PanAccessSessionError,
        PanAccessRateLimitError,
        ConnectionError,
    ),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def sync_subscribers_task(self, limit=None):
    if RedisConfig.is_full_sync_in_progress():
        return _skipped_during_full_sync("sync_subscribers_task")

    lock_key = "celery:lock:sync_subscribers_task"
    lock_timeout = 600

    with RedisConfig.task_lock(lock_key, timeout=lock_timeout) as acquired:
        if not acquired:
            logger.warning(
                "⚠️ [Celery] sync_subscribers_task ya está ejecutándose, saltando esta ejecución"
            )
            return {
                "success": False,
                "message": "Task already running, skipped",
                "skipped": True,
            }

        try:
            env_limit = CeleryConfig.SYNC_LIMIT
            limit = limit or env_limit or 200

            logger.info("🔄 [Celery] Iniciando sync_subscribers_task con limit=%s", limit)
            result = sync_subscribers(limit=limit)
            logger.info("✅ [Celery] Sincronización completada")
            return {
                "success": True,
                "limit": limit,
                "result": result,
            }
        except PanAccessException:
            logger.error("❌ [Celery] Error de PanAccess")
            raise
        except Exception:
            logger.exception("💥 [Celery] Error inesperado en sync_subscribers_task")
            raise


@shared_task(
    bind=True,
    autoretry_for=(
        PanAccessConnectionError,
        PanAccessTimeoutError,
        PanAccessSessionError,
        PanAccessRateLimitError,
        ConnectionError,
    ),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def sync_products_task(self, limit=None):
    if RedisConfig.is_full_sync_in_progress():
        return _skipped_during_full_sync("sync_products_task")

    lock_key = "celery:lock:sync_products_task"
    lock_timeout = 600

    with RedisConfig.task_lock(lock_key, timeout=lock_timeout) as acquired:
        if not acquired:
            logger.warning("[Celery] sync_products_task ya está ejecutándose, se omite")
            return {
                "success": False,
                "message": "Task already running, skipped",
                "skipped": True,
            }

        try:
            env_limit = CeleryConfig.SYNC_LIMIT
            limit = limit or env_limit or 200
            logger.info("[Celery] Iniciando sync_products_task limit=%s", limit)
            result = sync_products(session_id=None, limit=limit)
            logger.info("[Celery] sync_products_task completada")
            return {"success": True, "limit": limit, "result": result}
        except PanAccessException:
            logger.error("[Celery] Error PanAccess en sync_products_task")
            raise
        except Exception:
            logger.exception("[Celery] Error inesperado en sync_products_task")
            raise


@shared_task(
    bind=True,
    autoretry_for=(
        PanAccessConnectionError,
        PanAccessTimeoutError,
        PanAccessSessionError,
        PanAccessRateLimitError,
        ConnectionError,
    ),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def compare_and_update_subscribers_task(self, limit=None):
    if RedisConfig.is_full_sync_in_progress():
        return _skipped_during_full_sync("compare_and_update_subscribers_task")

    lock_key = "celery:lock:compare_and_update_subscribers_task"
    # El TTL del lock debe cubrir al menos lo que dura el time_limit de la
    # tarea (ver CELERY_BEAT_SCHEDULE["compare-subscribers-frequent"] en
    # settings.py) -- si el lock expirara antes de que termine una corrida
    # larga, un disparo posterior de Beat podría creer que está libre y
    # arrancar una segunda corrida en paralelo sobre el mismo catálogo.
    lock_timeout = CeleryConfig.COMPARE_SUBSCRIBERS_LOCK_TIMEOUT

    with RedisConfig.task_lock(lock_key, timeout=lock_timeout) as acquired:
        if not acquired:
            logger.warning(
                "[Celery] compare_and_update_subscribers_task ya está ejecutándose, se omite"
            )
            return {
                "success": False,
                "message": "Task already running, skipped",
                "skipped": True,
            }

        try:
            env_limit = CeleryConfig.SYNC_LIMIT
            limit = limit or env_limit or 1000

            logger.info(
                "[Celery] Iniciando compare_and_update_subscribers_task limit=%s", limit
            )
            result = compare_and_update_all_subscribers(session_id=None, limit=limit)
            logger.info("[Celery] compare_and_update_subscribers_task completada")
            return {"success": True, "limit": limit, "result": result}
        except PanAccessException:
            logger.error("[Celery] Error PanAccess en compare subscribers")
            raise
        except Exception:
            logger.exception(
                "[Celery] Error inesperado en compare_and_update_subscribers_task"
            )
            raise


@shared_task(
    bind=True,
    autoretry_for=(
        PanAccessConnectionError,
        PanAccessTimeoutError,
        PanAccessSessionError,
        PanAccessRateLimitError,
        ConnectionError,
    ),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def compare_and_update_smartcards_task(self, limit=None):
    if RedisConfig.is_full_sync_in_progress():
        return _skipped_during_full_sync("compare_and_update_smartcards_task")

    lock_key = "celery:lock:compare_and_update_smartcards_task"
    lock_timeout = 600

    with RedisConfig.task_lock(lock_key, timeout=lock_timeout) as acquired:
        if not acquired:
            logger.warning(
                "[Celery] compare_and_update_smartcards_task ya está ejecutándose, se omite"
            )
            return {
                "success": False,
                "message": "Task already running, skipped",
                "skipped": True,
            }

        try:
            env_limit = CeleryConfig.SYNC_LIMIT
            limit = limit or env_limit or 200

            logger.info(
                "[Celery] Iniciando compare_and_update_smartcards_task limit=%s", limit
            )
            result = compare_and_update_all_smartcards(session_id=None, limit=limit)
            logger.info("[Celery] compare_and_update_smartcards_task completada")
            return {"success": True, "limit": limit, "result": result}
        except PanAccessException:
            logger.error("[Celery] Error PanAccess en compare smartcards")
            raise
        except Exception:
            logger.exception(
                "[Celery] Error inesperado en compare_and_update_smartcards_task"
            )
            raise


@shared_task(
    bind=True,
    autoretry_for=(
        PanAccessConnectionError,
        PanAccessTimeoutError,
        PanAccessSessionError,
        PanAccessRateLimitError,
        ConnectionError,
    ),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def sync_smartcards_task(self, limit=None):
    if RedisConfig.is_full_sync_in_progress():
        return _skipped_during_full_sync("sync_smartcards_task")

    lock_key = "celery:lock:sync_smartcards_task"
    lock_timeout = 600

    with RedisConfig.task_lock(lock_key, timeout=lock_timeout) as acquired:
        if not acquired:
            logger.warning(
                "[Celery] sync_smartcards_task ya está ejecutándose, se omite"
            )
            return {
                "success": False,
                "message": "Task already running, skipped",
                "skipped": True,
            }

        try:
            env_limit = CeleryConfig.SYNC_LIMIT
            limit = limit or env_limit or 200

            logger.info("[Celery] Iniciando sync_smartcards_task limit=%s", limit)
            result = sync_smartcards(limit=limit)
            logger.info("[Celery] sync_smartcards_task completada")
            return {"success": True, "limit": limit, "result": result}
        except PanAccessException:
            logger.error("[Celery] Error PanAccess")
            raise
        except Exception:
            logger.exception("[Celery] Error inesperado en sync_smartcards_task")
            raise


@shared_task(
    bind=True,
    autoretry_for=(
        PanAccessConnectionError,
        PanAccessTimeoutError,
        PanAccessSessionError,
        PanAccessRateLimitError,
        ConnectionError,
    ),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def full_sync_task(self, limit=None):
    lock_key = "celery:lock:full_sync_task"
    lock_timeout = CeleryConfig.FULL_SYNC_TIME_LIMIT

    with RedisConfig.task_lock(lock_key, timeout=lock_timeout) as acquired:
        if not acquired:
            logger.warning("[Celery] full_sync_task ya está ejecutándose, se omite")
            return {"success": False, "skipped": True, "message": "Task already running"}

        RedisConfig.set_full_sync_in_progress(timeout=lock_timeout)
        try:
            env_limit = CeleryConfig.SYNC_LIMIT
            limit = limit or env_limit or 200
            started = time.monotonic()
            logger.info("[Celery] Iniciando full_sync_task con limit=%s", limit)
            result = run_full_sync(limit=limit)
            elapsed = time.monotonic() - started
            logger.info(
                "[Celery] full_sync_task completada en %.1fs success=%s",
                elapsed,
                result.get("success") if isinstance(result, dict) else result,
            )
            if isinstance(result, dict):
                result["duration_seconds"] = round(elapsed, 1)
            return result
        except PanAccessException:
            logger.error("[Celery] Error PanAccess en full_sync_task")
            raise
        except Exception:
            logger.exception("[Celery] Error inesperado en full_sync_task")
            raise
        finally:
            RedisConfig.clear_full_sync_in_progress()


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def finish_subscriber_provisioning_task(
    self,
    *,
    subscriber_code,
    data,
    email_normalized,
    phone_normalized,
    user_provided_code,
    grant_registration_trial,
    request_extra,
    is_social_account,
):
    """
    Continúa el registro de un suscriptor ya creado en PanAccess
    (`addSubscriber` ya corrió sync en `create_subscriber_view`): registros
    de unicidad local, contactos (email/teléfono), license block y producto
    de prueba. Solo se dispara si `FeatureConfig.CREATE_SUBSCRIBER_ASYNC_ENRICHMENT`
    está activado -- ver `wind/functions/create_subscriber.py`.

    Nota: en este modo el cliente ya recibió su respuesta (201) sin
    "token"/"credentials_url"/"license_block_added" -- esos ya no se pueden
    devolver sync porque dependen de estas mismas llamadas. El correo de
    bienvenida (ya async por su cuenta) sigue siendo el mecanismo para que
    el usuario reciba sus credenciales.
    """
    from datetime import timedelta

    from appConfig import PanaccessConfig
    from wind.exceptions import PanAccessException
    from wind.functions.create_subscriber import (
        _persist_subscriber_contacts_to_db,
        _resolve_email_contact_id,
        _validate_email_contact_of_subscriber,
    )
    from wind.functions.getSubscriber import (
        CallListExtendedSubscribers,
        extract_first_email,
        extract_first_phone,
    )
    from wind.models import ListOfSubscriber, SubscriberDocumentRegistry, SubscriberEmailRegistry
    from wind.services import get_panaccess
    from wind.services.subscriber_trial import mark_trial_granted, registration_trial_days
    from wind.services.welcome_email import enqueue_welcome_credentials_email

    panaccess = get_panaccess()
    result = {"subscriber_code": subscriber_code}

    # 1) Registros locales de unicidad (idénticos a los que ya hacía la
    #    ruta síncrona antes de este cambio).
    SubscriberEmailRegistry.objects.update_or_create(
        email=email_normalized,
        defaults={
            "subscriber_code": subscriber_code,
            "account_closed_at": None,
            "closed_subscriber_code": None,
        },
    )
    if user_provided_code:
        SubscriberDocumentRegistry.objects.update_or_create(
            document=user_provided_code,
            defaults={
                "subscriber_code": subscriber_code,
                "email": email_normalized,
                "account_closed_at": None,
                "closed_subscriber_code": None,
            },
        )

    # 2) Buscar el suscriptor recién creado en el catálogo para traer sus
    #    smartcards y guardar la fila local (ListOfSubscriber).
    def _find_in_catalog():
        offset = 0
        limit = 100
        for _ in range(3):
            try:
                page = CallListExtendedSubscribers(session_id=None, offset=offset, limit=limit)
            except Exception:
                logger.exception("[Async] Error buscando %s en catálogo PanAccess", subscriber_code)
                return None
            rows = page.get("extendedSubscriberEntries") or page.get("subscriberEntries") or page.get("rows", [])
            for row in rows:
                if row.get("subscriberCode") == subscriber_code:
                    return row
            if len(rows) < limit:
                break
            offset += limit
        return None

    found = _find_in_catalog()
    smartcards_list = found.get("smartcards") if found else None
    subscriber_obj = None
    if found:
        try:
            subscriber_obj, _created = ListOfSubscriber.objects.update_or_create(
                code=subscriber_code,
                defaults={
                    "id": subscriber_code,
                    "lastName": found.get("lastName"),
                    "firstName": found.get("firstName"),
                    "smartcards": smartcards_list,
                    "regionId": found.get("regionId") or request_extra.get("regionId"),
                    "countryCode": found.get("countryCode") or request_extra.get("countryCode"),
                    "caf": found.get("caf") or request_extra.get("caf"),
                    "supervisor": found.get("supervisor", "AUTOMATICO"),
                    "comment": found.get("comment") or data.get("comment"),
                    "emails": extract_first_email(found.get("emails")) or email_normalized,
                    "phones": extract_first_phone(found.get("phones")),
                    "created": timezone.now(),
                },
            )
        except Exception:
            logger.exception("[Async] Error guardando %s en ListOfSubscriber", subscriber_code)
    else:
        logger.warning("[Async] No se encontró %s en el catálogo de PanAccess tras crearlo", subscriber_code)
    result["found_in_catalog"] = bool(found)

    # 3) Contactos: email (+ validación) y teléfono si vino.
    contacts_added = []
    try:
        add_resp = panaccess.call(
            "addContactToSubscriber",
            {"code": subscriber_code, "type": "email", "isBusiness": False, "contact": email_normalized},
        )
        if add_resp.get("success"):
            contacts_added.append("email")
            contact_id = _resolve_email_contact_id(panaccess, subscriber_code, add_resp, email_normalized)
            if contact_id is not None:
                _validate_email_contact_of_subscriber(panaccess, subscriber_code, contact_id, email_normalized)
        else:
            logger.error("[Async] addContactToSubscriber (email) falló para %s: %s", subscriber_code, add_resp.get("errorMessage"))
    except PanAccessException:
        logger.exception("[Async] Error PanAccess agregando contacto email para %s", subscriber_code)

    if phone_normalized:
        try:
            phone_resp = panaccess.call(
                "addContactToSubscriber",
                {"code": subscriber_code, "type": "phone", "isBusiness": False, "contact": phone_normalized},
            )
            if phone_resp.get("success"):
                contacts_added.append("phone")
            else:
                logger.error("[Async] addContactToSubscriber (phone) falló para %s: %s", subscriber_code, phone_resp.get("errorMessage"))
        except PanAccessException:
            logger.exception("[Async] Error PanAccess agregando contacto phone para %s", subscriber_code)

    _persist_subscriber_contacts_to_db(subscriber_code, email=email_normalized, phone=phone_normalized)
    result["contacts_added"] = contacts_added

    # 4) License block + producto de prueba (solo si quedaron smartcards).
    license_block_success = False
    try:
        license_resp = panaccess.call("addLicenseBlockToSubscriber", {"code": subscriber_code})
        license_block_success = bool(license_resp.get("success"))
        if not license_block_success:
            logger.warning("[Async] addLicenseBlockToSubscriber falló para %s: %s", subscriber_code, license_resp.get("errorMessage"))
    except PanAccessException:
        logger.exception("[Async] Error PanAccess en license block para %s", subscriber_code)
    result["license_block_added"] = license_block_success

    if license_block_success:
        found_after = _find_in_catalog()
        updated_smartcards = found_after.get("smartcards") if found_after else None
        if updated_smartcards and subscriber_obj is not None:
            try:
                subscriber_obj.smartcards = updated_smartcards
                subscriber_obj.save(update_fields=["smartcards"])
            except Exception:
                logger.exception("[Async] Error actualizando smartcards de %s tras license block", subscriber_code)

            if grant_registration_trial:
                try:
                    trial_days = registration_trial_days()
                    expiry_time = timezone.now() + timedelta(days=trial_days)
                    product_params = {
                        "productId": PanaccessConfig.REGISTRATION_PRODUCT_ID,
                        "hcId": PanaccessConfig.HCID,
                        "expiryTime": expiry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    for idx, sn in enumerate(updated_smartcards):
                        if sn:
                            product_params[f"smartcards[{idx}]"] = str(sn)
                    product_resp = panaccess.call("addProductToSmartcards", product_params)
                    if product_resp.get("success"):
                        mark_trial_granted(
                            email=email_normalized,
                            document=user_provided_code or None,
                            subscriber_code=subscriber_code,
                            granted_at=timezone.now(),
                        )
                        result["trial_granted"] = True
                    else:
                        logger.warning("[Async] addProductToSmartcards falló para %s: %s", subscriber_code, product_resp.get("errorMessage"))
                except PanAccessException:
                    logger.exception("[Async] Error PanAccess asignando producto de prueba a %s", subscriber_code)
            else:
                logger.info("[Async] Trial omitido para %s (no elegible)", subscriber_code)

    # 5) Correo de bienvenida (ya es async por su cuenta).
    try:
        enqueue_welcome_credentials_email(
            first_name=data.get("firstName", ""),
            last_name=data.get("lastName", ""),
            email=email_normalized,
            subscriber_code=subscriber_code,
            is_social_account=is_social_account,
        )
    except Exception:
        logger.warning("[Async] No se pudo encolar el correo de bienvenida para %s", subscriber_code, exc_info=True)

    logger.info("[Async] Aprovisionamiento adicional completado para %s: %s", subscriber_code, result)
    return {"success": True, **result}


@shared_task(bind=True)
def retry_partial_closures_task(self):
    """
    Reintenta cierres de cuenta que quedaron a medias en PanAccess
    (ListOfSubscriber.status == PENDING_CLOSURE, nunca llegó a CLOSED).

    close_subscriber_account ya es idempotente (deja un tombstone
    PENDING_CLOSURE desde el primer intento, y solo pasa a CLOSED si
    PanAccess responde success), así que reintentar es simplemente volver a
    llamarlo. Cada suscriptor lleva su propio contador
    (closure_retry_count); al llegar a CLOSURE_RETRY_MAX_ATTEMPTS se deja de
    reintentar automáticamente y se manda una alerta por correo para que
    alguien lo revise a mano.
    """
    from appConfig import CeleryConfig, EmailConfig
    from wind.models import ListOfSubscriber
    from wind.services.subscriber_closure import close_subscriber_account

    if not CeleryConfig.CLOSURE_RETRY_ENABLED:
        return {"success": True, "skipped": True, "message": "CLOSURE_RETRY_ENABLED=false"}

    max_attempts = CeleryConfig.CLOSURE_RETRY_MAX_ATTEMPTS
    stuck = ListOfSubscriber.objects.filter(
        status=ListOfSubscriber.STATUS_PENDING_CLOSURE,
        closure_retry_count__lt=max_attempts,
    )

    retried = []
    exhausted = []

    for subscriber in stuck:
        code = subscriber.code or subscriber.id
        logger.info(
            "[Celery] Reintentando cierre parcial de %s (intento %s/%s)",
            code,
            subscriber.closure_retry_count + 1,
            max_attempts,
        )
        try:
            result = close_subscriber_account(code, reason="retry_automatico_cierre_parcial")
        except Exception:
            logger.exception("[Celery] Error inesperado reintentando cierre de %s", code)
            result = {"success": False}

        if result.get("success"):
            retried.append(code)
            logger.info("[Celery] Reintento de cierre exitoso para %s", code)
            continue

        subscriber.closure_retry_count += 1
        subscriber.save(update_fields=["closure_retry_count"])

        if subscriber.closure_retry_count >= max_attempts:
            exhausted.append(code)
            logger.error(
                "[Celery] Cierre de %s sigue parcial tras %s intentos; se deja de reintentar automáticamente",
                code,
                subscriber.closure_retry_count,
            )
            _alert_closure_exhausted(code, subscriber.closure_retry_count, EmailConfig.OPS_ALERT_ADDRESS)

    return {
        "success": True,
        "checked": stuck.count(),
        "retried_ok": retried,
        "exhausted": exhausted,
    }


def _alert_closure_exhausted(subscriber_code: str, attempts: int, to_address: str) -> None:
    if not to_address:
        return
    try:
        send_verification_email_task.delay(
            to_address,
            f"[Wind] Cierre de cuenta {subscriber_code} sigue parcial tras {attempts} intentos",
            (
                f"El cierre de cuenta del suscriptor {subscriber_code} lleva {attempts} "
                "intentos automáticos fallidos y quedó en PENDING_CLOSURE. "
                "Revisar SubscriberClosureLog para ese código y reintentar manualmente "
                "(python manage.py close_subscriber --code "
                f"{subscriber_code} --reason \"retry manual\")."
            ),
        )
    except Exception:
        logger.exception("No se pudo encolar la alerta de cierre agotado para %s", subscriber_code)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_welcome_credentials_email_task(self, email, subject, text_body, html_body):
    """
    Envía el correo de bienvenida con credenciales tras el registro.
    """
    from django.core.mail import send_mail
    from django.conf import settings

    logger.info("Enviando correo de bienvenida a %s", email)
    try:
        send_mail(
            subject=subject,
            message=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
            html_message=html_body,
        )
        logger.info("Correo de bienvenida enviado a %s", email)
        return {"success": True, "email": email}
    except Exception as exc:
        logger.exception("Error al enviar correo de bienvenida a %s", email)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return {"success": False, "error": str(exc), "email": email}


@shared_task
def send_password_reset_email_task(email: str, reset_link: str):
    """Envía el correo con enlace de recuperación de contraseña."""
    from django.conf import settings
    from django.core.mail import send_mail

    subject = "Restablecer contraseña - Wind"
    body = (
        "Hola,\n\n"
        "Recibimos una solicitud para restablecer la contraseña de tu cuenta Wind.\n"
        f"Usa este enlace (válido 60 minutos):\n\n{reset_link}\n\n"
        "Si no solicitaste este cambio, ignora este correo.\n"
    )
    html_body = (
        "<p>Hola,</p>"
        "<p>Recibimos una solicitud para restablecer la contraseña de tu cuenta Wind.</p>"
        f'<p><a href="{reset_link}">Restablecer contraseña</a></p>'
        "<p>El enlace expira en 60 minutos.</p>"
        "<p>Si no solicitaste este cambio, ignora este correo.</p>"
    )
    logger.info("Enviando email de recuperación de contraseña a %s", email)
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
            html_message=html_body,
        )
        logger.info("Email de recuperación enviado a %s", email)
        return {"success": True, "email": email}
    except Exception as e:
        logger.exception("Error al enviar email de recuperación a %s", email)
        return {"success": False, "error": str(e), "email": email}


@shared_task
def send_verification_email_task(email, subject, body, html_body=None):
    """
    Envía un email de verificación o notificación de forma asíncrona.
    """
    from django.core.mail import send_mail
    from django.conf import settings
    logger.info("Enviando email de verificación a %s", email)
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
            html_message=html_body,
        )
        logger.info("Email enviado exitosamente a %s", email)
        return {"success": True, "email": email}
    except Exception as e:
        logger.exception("Error al enviar email a %s", email)
        return {"success": False, "error": str(e), "email": email}
