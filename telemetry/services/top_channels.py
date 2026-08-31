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
import json
import logging
from datetime import timedelta
from typing import Any, Dict, List

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

from appConfig import MostWatchedChannelsConfig
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

# El ranking se entrega como si fuera un bouquet más de PanAccess (ver
# "Documentación configuración de bouquets" del cliente) -- bouquetId/name
# fijos en código; el card_design sí es configurable (ver appConfig).
TOP_CHANNELS_BOUQUET_ID = "most-watched"
TOP_CHANNELS_BOUQUET_NAME = "Más vistos"
# `priority`/`isMain` -- mismos campos que trae un bouquet real de PanAccess
# y que appVideo ya sabe leer para ordenar el muro de Inicio
# (`tvDataService.js`: prioridad ascendente, "0" primero; `isMain` decide
# si el bouquet entra en Inicio vs. "Servicios TV y Radio"). "0" como
# string a propósito -- `tvDataService.js` lo castea con `Number(...)`, así
# que da lo mismo que un int, pero así queda igual de tipo que como
# PanAccess manda esta clave en los bouquets reales.
TOP_CHANNELS_BOUQUET_PRIORITY = "0"
TOP_CHANNELS_BOUQUET_IS_MAIN = True


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

    # total_duration_seconds/total_views solo se usan arriba para ordenar --
    # appVideo nunca los lee (cruza únicamente por channel_id), así que no
    # se exponen en la respuesta.
    return [
        {
            "rank": i + 1,
            "channel_id": row["channel_id"],
            "name": names.get(row["channel_id"]),
        }
        for i, row in enumerate(rows)
    ]


def _build_top_channels_custom_data() -> str:
    """
    `customData` del "bouquet" de más vistos, con la misma forma que
    documentó el cliente para bouquets reales de PanAccess (layouts.mobile /
    layouts.tv). Mismo diseño en mobile y tv -- solo card_design es
    configurable por env (`MostWatchedChannelsConfig`, ver appConfig.py).
    """
    layout = {
        "type": "horizontal_grid",
        "rows": 1,
        "card_design": MostWatchedChannelsConfig.TOP_CHANNELS_CARD_DESIGN,
        "logo_index": "1",
    }
    return json.dumps({"layouts": {"mobile": layout, "tv": layout}})


def build_top_channels_bouquet(
    window_days: int = DEFAULT_WINDOW_DAYS, limit: int = DEFAULT_LIMIT
) -> Dict[str, Any]:
    """
    Envuelve el ranking (lectura desde caché, ver `get_top_channels_global`)
    con la forma de un bouquet de PanAccess -- lo que efectivamente devuelve
    el endpoint a las apps.
    """
    return {
        "bouquetId": TOP_CHANNELS_BOUQUET_ID,
        "name": TOP_CHANNELS_BOUQUET_NAME,
        "priority": TOP_CHANNELS_BOUQUET_PRIORITY,
        "isMain": TOP_CHANNELS_BOUQUET_IS_MAIN,
        "customData": _build_top_channels_custom_data(),
        "channels": get_top_channels_global(window_days=window_days, limit=limit),
    }


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
