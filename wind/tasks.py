import logging
import time

from celery import shared_task

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
    lock_timeout = 600

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
            limit = limit or env_limit or 200

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
