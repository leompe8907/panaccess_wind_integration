"""
Ranking de "canales más vistos" (global) -- lo consumen appVideo (vía el
endpoint de telemetry/views.py) para armar el bouquet/riel de más vistos.

Diseño clave: el caché NUNCA se genera "perezosamente" en el momento de
la petición. Lo escribe `aggregate_ott_channels_task` (telemetry/tasks.py)
cada vez que termina de recalcular los agregados diarios. El endpoint
solo lee lo que ya está en Redis -- así una expiración de caché nunca
puede coincidir con miles de usuarios pidiendo el ranking al mismo
tiempo y disparando el cálculo pesado a la vez (cache stampede). El TTL
que se le pone al caché es solo un resguardo (si la tarea periódica
alguna vez dejara de correr, preferible que el dato desaparezca a que
quede sirviendo algo eternamente viejo sin que nadie lo note).
"""
import logging
from datetime import timedelta
from typing import Any, Dict, List

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

from telemetry.models import TelemetryChannelDailyAgg, TelemetryOttChannelName

logger = logging.getLogger(__name__)

CACHE_KEY = "telemetry:top_channels:global:v1"
# Resguardo: bastante más que el intervalo de la tarea de agregación
# (default 60 min) para no depender de que ambos valores estén siempre
# perfectamente sincronizados, pero corto para "cuando se rompe, el JSON
# desaparece" es un observable claro.
CACHE_TTL_SECONDS = 6 * 60 * 60

DEFAULT_WINDOW_DAYS = 7
DEFAULT_LIMIT = 10  # confirmado con el cliente: top 10 a nivel general


def compute_top_channels_global(
    window_days: int = DEFAULT_WINDOW_DAYS, limit: int = DEFAULT_LIMIT
) -> List[Dict[str, Any]]:
    """
    Suma TelemetryChannelDailyAgg de los últimos `window_days` días y
    ordena por tiempo total visto (mejor proxy que cantidad de "views" --
    un view de 5 segundos no debería pesar igual que uno de 2 horas).
    """
    start_date = timezone.localdate() - timedelta(days=window_days)

    rows = (
        TelemetryChannelDailyAgg.objects.filter(day__gte=start_date)
        .values("channel_id")
        .annotate(
            total_duration_seconds=Sum("total_duration_seconds"),
            total_views=Sum("views"),
        )
        .order_by("-total_duration_seconds")[:limit]
    )
    rows = list(rows)

    channel_ids = [r["channel_id"] for r in rows]
    names = dict(
        TelemetryOttChannelName.objects.filter(channel_id__in=channel_ids).values_list(
            "channel_id", "name"
        )
    )

    return [
        {
            "rank": i + 1,
            "channel_id": row["channel_id"],
            "name": names.get(row["channel_id"]),
            "total_duration_seconds": row["total_duration_seconds"] or 0,
            "total_views": row["total_views"] or 0,
        }
        for i, row in enumerate(rows)
    ]


def refresh_top_channels_cache(
    window_days: int = DEFAULT_WINDOW_DAYS, limit: int = DEFAULT_LIMIT
) -> List[Dict[str, Any]]:
    """Recalcula el ranking y lo escribe en caché. Pensada para llamarse
    desde aggregate_ott_channels_task, no desde el endpoint."""
    top_channels = compute_top_channels_global(window_days=window_days, limit=limit)
    cache.set(CACHE_KEY, top_channels, timeout=CACHE_TTL_SECONDS)
    logger.info("Caché de top-channels actualizado: %s canales", len(top_channels))
    return top_channels


def get_top_channels_global(
    window_days: int = DEFAULT_WINDOW_DAYS, limit: int = DEFAULT_LIMIT
) -> List[Dict[str, Any]]:
    """
    Lectura para el endpoint: caché primero. Si no hay nada en caché
    (recién desplegado, o la tarea periódica todavía no corrió una
    primera vez), calcula al vuelo como respaldo -- no falla la
    respuesta, pero no es el camino esperado en operación normal.
    """
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    logger.warning(
        "Caché de top-channels vacío -- calculando al vuelo como respaldo "
        "(revisar que telemetry-aggregate-ott esté corriendo)."
    )
    return compute_top_channels_global(window_days=window_days, limit=limit)
