import logging

from celery import shared_task
from django.utils import timezone

from appConfig import AppLogsConfig

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def purge_old_log_events_task(self):
    """
    Retención de logs de diagnóstico (`applogs`) -- ver
    docs/LOGS_DIAGNOSTICO_2026-09-01.md. Borra solo `LogEvent` (las
    ocurrencias individuales, con su stack/breadcrumbs/contexto de
    dispositivo) más viejos que `AppLogsConfig.RETENTION_DAYS`. El
    `LogIssue` agrupado NUNCA se borra acá -- es el historial liviano
    ("este error existió, se vio N veces entre tal fecha y tal otra"), y
    sirve para no perder el conteo total aunque se pode el detalle de cada
    ocurrencia.
    """
    from applogs.models import LogEvent

    if not AppLogsConfig.RETENTION_ENABLED:
        return {"success": True, "skipped": True, "message": "APP_LOGS_RETENTION_ENABLED=false"}

    cutoff = timezone.now() - timezone.timedelta(days=AppLogsConfig.RETENTION_DAYS)
    deleted, _ = LogEvent.objects.filter(created_at__lt=cutoff).delete()

    if deleted:
        logger.info("applogs: purga de retención -- %s LogEvent borrados (cutoff=%s)", deleted, cutoff.isoformat())

    return {"success": True, "deleted": deleted, "cutoff": cutoff.isoformat()}
