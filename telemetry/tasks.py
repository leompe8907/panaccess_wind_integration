import logging

from celery import shared_task

from wind.exceptions import (
    PanAccessConnectionError,
    PanAccessException,
    PanAccessRateLimitError,
    PanAccessSessionError,
    PanAccessTimeoutError,
)
from appConfig import CeleryConfig, RedisConfig

from telemetry.services.panaccess_ott_ingest import ingest_new_ott_records
from telemetry.services.aggregate_ott import aggregate_ott_channels
from telemetry.services.top_channels import refresh_top_channels_cache

logger = logging.getLogger(__name__)


def _skipped_already_running(task_name: str) -> dict:
    logger.warning("[Celery] %s ya está ejecutándose, se omite", task_name)
    return {"success": False, "skipped": True, "message": "Task already running, skipped"}


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
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def ingest_ott_telemetry_task(self):
    """
    Trae de PanAccess los eventos OTT nuevos (actionId 7/8, filtrados
    server-side por recordId > cursor) y los guarda. Ver
    telemetry/services/panaccess_ott_ingest.py.
    """
    lock_key = "celery:lock:telemetry:ingest_ott_telemetry_task"
    lock_timeout = CeleryConfig.TELEMETRY_INGEST_LOCK_TIMEOUT

    with RedisConfig.task_lock(lock_key, timeout=lock_timeout, auto_extend=True) as acquired:
        if not acquired:
            return _skipped_already_running("ingest_ott_telemetry_task")

        try:
            logger.info("[Celery] Iniciando ingest_ott_telemetry_task")
            result = ingest_new_ott_records()
            logger.info("[Celery] ingest_ott_telemetry_task completada: %s", result)
            return {"success": True, "result": result}
        except PanAccessException:
            logger.error("[Celery] Error PanAccess en ingest_ott_telemetry_task")
            raise
        except Exception:
            logger.exception("[Celery] Error inesperado en ingest_ott_telemetry_task")
            raise


@shared_task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=120)
def aggregate_ott_channels_task(self):
    """
    Recalcula los agregados diarios (TelemetryChannelDailyAgg,
    TelemetryUserChannelDailyAgg) sobre hoy + un margen de días hacia
    atrás. No llama a PanAccess -- solo lee TelemetryOttViewEvent, que ya
    está en la base local. Ver telemetry/services/aggregate_ott.py.
    """
    lock_key = "celery:lock:telemetry:aggregate_ott_channels_task"
    lock_timeout = CeleryConfig.TELEMETRY_AGGREGATE_LOCK_TIMEOUT

    with RedisConfig.task_lock(lock_key, timeout=lock_timeout, auto_extend=True) as acquired:
        if not acquired:
            return _skipped_already_running("aggregate_ott_channels_task")

        try:
            logger.info("[Celery] Iniciando aggregate_ott_channels_task")
            result = aggregate_ott_channels(days_back=CeleryConfig.TELEMETRY_AGGREGATE_DAYS_BACK)

            # El caché del ranking se refresca acá, no en el endpoint --
            # así una expiración de caché nunca coincide con tráfico de
            # usuarios pidiendo el ranking al mismo tiempo (ver
            # telemetry/services/top_channels.py).
            try:
                refresh_top_channels_cache()
            except Exception:
                # No tumbar la tarea completa por un fallo al escribir el
                # caché -- los agregados ya quedaron guardados; el
                # endpoint tiene su propio respaldo si el caché falta.
                logger.exception("[Celery] Error refrescando caché de top-channels")

            logger.info("[Celery] aggregate_ott_channels_task completada: %s", result)
            return {"success": True, "result": result}
        except Exception:
            logger.exception("[Celery] Error inesperado en aggregate_ott_channels_task")
            raise
