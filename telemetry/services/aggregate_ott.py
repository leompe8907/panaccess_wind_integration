"""
Agregación diaria de eventos OTT -- alimenta el ranking de "canales más
vistos" (global y, a futuro, personalizado).

Recalcula por completo el rango [hoy - days_back, hoy] en cada corrida
en vez de llevar contadores incrementales: es más simple y correcto
(cubre eventos que lleguen desordenados/tarde), y es barato porque
`TelemetryOttViewEvent` ya está indexado por `event_date`.

Usa `bulk_create(update_conflicts=True, ...)` (upsert nativo desde
Django 4.1) en vez de `update_or_create` fila por fila -- con muchos
canales/suscriptores por día, hacer un upsert por fila sería cientos o
miles de queries individuales; esto lo resuelve en una sola sentencia
por tabla.
"""
import logging
from datetime import timedelta
from typing import Any, Dict

from django.db.models import Count, Sum
from django.utils import timezone

from telemetry.models import (
    TelemetryChannelDailyAgg,
    TelemetryOttViewEvent,
    TelemetryUserChannelDailyAgg,
)

logger = logging.getLogger(__name__)

# Cuántos días hacia atrás se recalculan en cada corrida, además de hoy.
# Cubre eventos que lleguen tarde o desordenados respecto a cuándo
# corrió la ingesta.
DEFAULT_DAYS_BACK = 2


def aggregate_ott_channels(days_back: int = DEFAULT_DAYS_BACK) -> Dict[str, Any]:
    today = timezone.localdate()
    start_date = today - timedelta(days=days_back)

    channel_rows = list(
        TelemetryOttViewEvent.objects.filter(event_date__gte=start_date)
        .values("event_date", "channel_id")
        .annotate(
            views=Count("id"),
            unique_subscribers=Count("subscriber_code", distinct=True),
            total_duration_seconds=Sum("duration_seconds"),
        )
    )

    if channel_rows:
        TelemetryChannelDailyAgg.objects.bulk_create(
            [
                TelemetryChannelDailyAgg(
                    day=row["event_date"],
                    channel_id=row["channel_id"],
                    views=row["views"],
                    unique_subscribers=row["unique_subscribers"],
                    total_duration_seconds=row["total_duration_seconds"] or 0,
                )
                for row in channel_rows
            ],
            update_conflicts=True,
            update_fields=["views", "unique_subscribers", "total_duration_seconds"],
            unique_fields=["day", "channel_id"],
        )

    user_channel_rows = list(
        TelemetryOttViewEvent.objects.filter(event_date__gte=start_date)
        .exclude(subscriber_code__isnull=True)
        .exclude(subscriber_code="")
        .values("event_date", "subscriber_code", "channel_id")
        .annotate(views=Count("id"), total_duration_seconds=Sum("duration_seconds"))
    )

    if user_channel_rows:
        TelemetryUserChannelDailyAgg.objects.bulk_create(
            [
                TelemetryUserChannelDailyAgg(
                    day=row["event_date"],
                    subscriber_code=row["subscriber_code"],
                    channel_id=row["channel_id"],
                    views=row["views"],
                    total_duration_seconds=row["total_duration_seconds"] or 0,
                )
                for row in user_channel_rows
            ],
            update_conflicts=True,
            update_fields=["views", "total_duration_seconds"],
            unique_fields=["day", "subscriber_code", "channel_id"],
        )

    result = {
        "start_date": str(start_date),
        "end_date": str(today),
        "channel_rows_upserted": len(channel_rows),
        "user_channel_rows_upserted": len(user_channel_rows),
    }
    logger.info("Agregación OTT completada: %s", result)
    return result
