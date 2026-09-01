"""
Servicio de ingesta de logs de diagnóstico -- ver docs/LOGS_DIAGNOSTICO_2026-09-01.md.

No es telemetría de negocio ni auditoría de seguridad (ver `applogs/apps.py`).
Un solo punto de entrada, `record_log_event()`, usado tanto por el endpoint
HTTP (`applogs/views.py`, para appVideo/iOS/Android) como por el handler de
logging del propio backend (`applogs/logging_handler.py`) -- así ambos
caminos agrupan y alertan exactamente igual.
"""
from __future__ import annotations

import hashlib
import logging

from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from appConfig import AppLogsConfig, EmailConfig
from applogs.models import LogEvent, LogIssue

logger = logging.getLogger(__name__)

# Cuánto de la primera línea del stack se usa para agrupar -- alcanza para
# distinguir "TypeError en X" de "TypeError en Y" sin que un stack gigante
# (con líneas de contexto que cambian por request, ej. IDs) rompa el
# agrupamiento de lo que en realidad es el mismo error.
_STACK_PREFIX_CHARS = 300
_MESSAGE_PREFIX_CHARS = 300


def compute_fingerprint(*, platform: str, level: str, message: str, stack: str = "") -> str:
    stack_head = (stack or "").strip().splitlines()[0] if (stack or "").strip() else ""
    raw = "|".join(
        [
            platform or "",
            level or "",
            (message or "")[:_MESSAGE_PREFIX_CHARS],
            stack_head[:_STACK_PREFIX_CHARS],
        ]
    )
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _get_or_create_issue(*, fingerprint: str, platform: str, level: str, message: str) -> tuple[LogIssue, bool]:
    try:
        issue = LogIssue.objects.get(fingerprint=fingerprint)
        return issue, False
    except LogIssue.DoesNotExist:
        pass
    try:
        with transaction.atomic():
            issue = LogIssue.objects.create(
                fingerprint=fingerprint,
                platform=platform,
                level=level,
                message=message[:500],
            )
        return issue, True
    except IntegrityError:
        # Carrera: otro request creó el mismo fingerprint entre el GET y el
        # CREATE de arriba -- mismo patrón que
        # wind.services.subscriber_preferences.get_or_migrate_preferences.
        return LogIssue.objects.get(fingerprint=fingerprint), False


def record_log_event(
    *,
    platform: str,
    level: str = LogIssue.LEVEL_ERROR,
    message: str,
    stack: str = "",
    breadcrumbs=None,
    extra=None,
    app_version: str = "",
    device_type: str = "",
    subscriber_code: str = "",
    client_ip: str | None = None,
) -> LogEvent:
    """
    Punto de entrada único de ingesta. Nunca debe lanzar hacia un caller que
    no pueda permitírselo (ej. el logging handler del propio backend) -- los
    callers HTTP (`applogs/views.py`) sí pueden dejar propagar una excepción
    real de datos inválidos, porque ahí ya se validó con el serializer antes
    de llegar acá.
    """
    fingerprint = compute_fingerprint(platform=platform, level=level, message=message, stack=stack)
    issue, is_new = _get_or_create_issue(fingerprint=fingerprint, platform=platform, level=level, message=message)

    now = timezone.now()
    issue = _bump_issue(issue, now=now)

    event = LogEvent.objects.create(
        issue=issue,
        subscriber_code=subscriber_code or "",
        device_type=device_type or "",
        app_version=app_version or "",
        stack=stack or "",
        breadcrumbs=breadcrumbs,
        extra=extra,
        client_ip=client_ip,
    )

    _maybe_alert(issue, is_new=is_new)

    return event


def _bump_issue(issue: LogIssue, *, now) -> LogIssue:
    from django.db.models import F

    LogIssue.objects.filter(pk=issue.pk).update(occurrence_count=F("occurrence_count") + 1, last_seen_at=now)
    issue.refresh_from_db(fields=["occurrence_count", "last_seen_at"])
    return issue


def _maybe_alert(issue: LogIssue, *, is_new: bool) -> None:
    """
    Alerta por email cuando aparece un `LogIssue` nuevo, o cuando uno ya
    conocido cruza un múltiplo de `AppLogsConfig.ALERT_SPIKE_EVERY`
    ocurrencias (para detectar picos de un error que ya se creía resuelto).
    Cooldown por issue (`ALERT_COOLDOWN_MINUTES`) para no saturar --
    nunca debe romper la ingesta si el envío falla.
    """
    if not AppLogsConfig.ALERTS_ENABLED:
        return
    recipients = AppLogsConfig.alert_recipients()
    if not recipients:
        return

    is_spike = (
        not is_new
        and AppLogsConfig.ALERT_SPIKE_EVERY > 0
        and issue.occurrence_count % AppLogsConfig.ALERT_SPIKE_EVERY == 0
    )
    if not (is_new or is_spike):
        return

    now = timezone.now()
    if issue.last_alerted_at is not None:
        cooldown_elapsed = (now - issue.last_alerted_at).total_seconds() / 60
        if cooldown_elapsed < AppLogsConfig.ALERT_COOLDOWN_MINUTES:
            return

    try:
        reason = "nuevo" if is_new else f"{issue.occurrence_count} ocurrencias"
        subject = f"[Wind logs] issue {reason} -- {issue.platform}/{issue.level}"
        body = (
            f"Plataforma: {issue.platform}\n"
            f"Nivel: {issue.level}\n"
            f"Mensaje: {issue.message}\n"
            f"Ocurrencias: {issue.occurrence_count}\n"
            f"Primera vez: {issue.first_seen_at}\n"
            f"Última vez: {issue.last_seen_at}\n"
            f"Fingerprint: {issue.fingerprint}\n"
        )
        send_mail(subject, body, EmailConfig.DEFAULT_FROM, recipients, fail_silently=True)
        LogIssue.objects.filter(pk=issue.pk).update(last_alerted_at=now)
    except Exception as exc:  # nunca romper la ingesta por un fallo de alerta
        logger.warning("applogs: no se pudo enviar alerta de issue #%s: %s", issue.pk, exc)
