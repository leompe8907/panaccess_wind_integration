"""
Ingesta de eventos OTT de telemetría desde PanAccess.

A diferencia del proyecto Telemetría original (que tenía su propio
cliente/sesión de PanAccess en `delancert/server/`), esta app reutiliza
el singleton ya existente en `wind.services.panaccess_singleton` --
mismo sessionId, mismo circuit breaker, mismo manejo de reautenticación
que usa el resto de Wind. No hay una sesión de PanAccess "propia" de
telemetría.

Flujo:
1. `fetch_new_ott_records()` pide a PanAccess SOLO lo nuevo: usa
   `newFilters` (soportado por `cvGetListOfTelemetryRecords`) para
   filtrar server-side por `(actionId=7 OR actionId=8) AND recordId >
   cursor`. PanAccess nunca manda registros viejos ni de otros tipos de
   evento (DVB/VOD/catchup) -- no hay que descartar nada del lado
   nuestro, ni en almacenamiento ni en tráfico de red.
2. `ingest_new_ott_records()` guarda esos registros: actionId=7 solo
   actualiza `TelemetryOttChannelName` (upsert, una fila por canal);
   actionId=8 se guarda como `TelemetryOttViewEvent` (la sesión de
   reproducción completa, con duración).
3. Al final se avanza el cursor al recordId más alto visto.
"""
import logging
import os
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone

from wind.exceptions import PanAccessAPIError, PanAccessException
from wind.services.panaccess_singleton import get_panaccess

from telemetry.models import (
    TelemetryIngestCursor,
    TelemetryOttChannelName,
    TelemetryOttViewEvent,
)

logger = logging.getLogger(__name__)

OTT_START_ACTION_ID = 7
OTT_STOP_ACTION_ID = 8
INGEST_SOURCE = "ott"

# Igual que en el proyecto original: el nombre exacto de la función varía
# por ambiente de PanAccess / permisos del token. Configurable por env,
# con fallbacks conocidos.
_DEFAULT_CANDIDATES = [
    "getListOfTelemetryRecords",
    "getListOfTelemetryRecordEntries",
    "getTelemetryRecords",
]


def _candidate_function_names() -> List[str]:
    env_func = os.getenv("PANACCESS_TELEMETRY_FUNCTION", "").strip()
    candidates = ([env_func] if env_func else []) + _DEFAULT_CANDIDATES
    seen = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


def _get_cursor() -> TelemetryIngestCursor:
    cursor, _ = TelemetryIngestCursor.objects.get_or_create(
        source=INGEST_SOURCE, defaults={"last_record_id": 0}
    )
    return cursor


def _build_new_filters(cursor_value: int) -> Dict[str, Any]:
    """
    (actionId = 7 OR actionId = 8) AND (recordId > cursor_value).

    Ver doc de cvGetListOfTelemetryRecords: `newFilters` combina grupos
    con su propio groupOp -- el groupOp de más afuera ("AND") decide
    cómo se combinan los dos grupos entre sí.
    """
    return {
        "groupOp": "AND",
        "filters": [
            {
                "groupOp": "OR",
                "rules": [
                    {"field": "actionId", "op": "eq", "data": str(OTT_START_ACTION_ID)},
                    {"field": "actionId", "op": "eq", "data": str(OTT_STOP_ACTION_ID)},
                ],
            },
            {
                "groupOp": "AND",
                "rules": [
                    {"field": "recordId", "op": "gt", "data": str(cursor_value)},
                ],
            },
        ],
    }


def _call_telemetry_api(offset: int, limit: int, cursor_value: int) -> Dict[str, Any]:
    """
    Llama a PanAccess probando los nombres de función candidatos, igual
    que el proyecto original -- pero sin reimplementar el manejo de
    sesión: `panaccess_singleton.call()` ya reautentica solo ante
    PanAccessSessionError.
    """
    panaccess = get_panaccess()
    parameters = {
        "offset": offset,
        "limit": min(limit, 1000),
        "orderBy": "recordId",
        "orderDir": "ASC",
        "newFilters": _build_new_filters(cursor_value),
    }

    last_permission_error: Optional[PanAccessAPIError] = None
    for func_name in _candidate_function_names():
        try:
            return panaccess.call(func_name=func_name, parameters=parameters, timeout=120)
        except PanAccessAPIError as e:
            if getattr(e, "error_code", None) == "no_access_to_function":
                logger.warning("Sin permisos para '%s', probando siguiente candidato...", func_name)
                last_permission_error = e
                continue
            raise

    raise PanAccessException(
        f"PanAccess denegó permisos para todas las funciones de telemetría probadas "
        f"({', '.join(_candidate_function_names())}). Detalle: {last_permission_error}"
    )


def fetch_new_ott_records(page_size: int = 1000, max_pages: int = 500) -> List[Dict[str, Any]]:
    """
    Descarga (paginando ASC) los registros OTT (actionId 7/8) con
    recordId mayor al cursor guardado. El filtro lo aplica PanAccess
    (`newFilters`), así que todo lo que llega ya es nuevo y ya es OTT --
    no hay que descartar nada acá, solo juntar páginas hasta que una
    venga incompleta (última página).
    """
    cursor_value = _get_cursor().last_record_id
    logger.info("Ingesta OTT: buscando registros nuevos desde recordId=%s", cursor_value)

    new_records: List[Dict[str, Any]] = []
    offset = 0

    for page in range(max_pages):
        response = _call_telemetry_api(offset=offset, limit=page_size, cursor_value=cursor_value)

        if not response.get("success"):
            raise PanAccessException(
                f"Error al obtener telemetría: {response.get('errorMessage', 'desconocido')}"
            )

        records = response.get("answer", {}).get("telemetryRecordEntries", [])
        if not records:
            break

        new_records.extend(records)

        if len(records) < page_size:
            break  # última página

        offset += page_size
    else:
        logger.warning(
            "Ingesta OTT: se alcanzó max_pages=%s -- puede quedar más por "
            "traer, la próxima corrida continúa desde el cursor actualizado.",
            max_pages,
        )

    logger.info("Ingesta OTT: %s registros nuevos descargados (ya filtrados a actionId 7/8)", len(new_records))
    return new_records


def _parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        naive = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None

    aware = timezone.make_aware(naive, timezone.get_current_timezone()).astimezone(dt_timezone.utc)
    # Mismo resguardo que el proyecto original: PanAccess a veces manda
    # timestamps corridos hacia el futuro (clock skew) -- eso rompe
    # agregados por día si se guarda tal cual.
    if aware > timezone.now() + timedelta(hours=24):
        logger.warning("Timestamp futuro descartado: %s", raw)
        return None
    return aware


def ingest_new_ott_records(page_size: int = 1000, max_pages: int = 500) -> Dict[str, int]:
    """
    Descarga y guarda los eventos OTT nuevos. Avanza el cursor solo si
    el guardado fue exitoso (si algo falla a medio camino, la próxima
    corrida vuelve a pedir desde el mismo punto -- `bulk_create` con
    `ignore_conflicts=True` hace esto seguro de repetir).
    """
    records = fetch_new_ott_records(page_size=page_size, max_pages=max_pages)
    if not records:
        return {"fetched": 0, "channel_names_upserted": 0, "view_events_attempted": 0}

    max_record_id_seen = 0
    channel_names: Dict[int, str] = {}
    view_events: List[TelemetryOttViewEvent] = []
    malformed_skipped = 0

    for record in records:
        record_id = record.get("recordId")
        if record_id is not None:
            max_record_id_seen = max(max_record_id_seen, record_id)

        action_id = record.get("actionId")
        data_id = record.get("dataId")

        if action_id == OTT_START_ACTION_ID:
            if data_id is not None and record.get("dataName"):
                # `records` viene en orden ASC (recordId creciente), así
                # que sobreescribimos sin problema: la última vez que
                # veamos un dataId en el lote es la más reciente.
                channel_names[data_id] = record["dataName"]
            continue

        if action_id != OTT_STOP_ACTION_ID:
            # No debería pasar -- PanAccess ya filtró a actionId 7/8 --
            # pero se deja como red de seguridad ante cualquier cambio.
            # Se loguea (auditoría, Medio #10): antes se descartaba en
            # silencio, sin ninguna forma de detectar si PanAccess empieza
            # a mandar un actionId inesperado.
            logger.warning(
                "Ingesta OTT: actionId inesperado %s (record_id=%s), fila descartada",
                action_id,
                record_id,
            )
            malformed_skipped += 1
            continue

        if data_id is None or record_id is None:
            # Evento STOP (actionId=8) sin los campos mínimos para armar
            # un TelemetryOttViewEvent -- antes se descartaba en silencio
            # (auditoría, Medio #10). Loguear cuál campo falta para poder
            # rastrear si es un problema recurrente del lado de PanAccess.
            logger.warning(
                "Ingesta OTT: evento STOP incompleto (data_id=%s, record_id=%s), fila descartada",
                data_id,
                record_id,
            )
            malformed_skipped += 1
            continue

        parsed_timestamp = _parse_timestamp(record.get("timestamp"))
        if parsed_timestamp is None:
            # Antes: si el timestamp era inválido/no parseable, el evento
            # se guardaba igual con event_date = fecha del SERVIDOR (hoy) --
            # eso puede distorsionar el ranking de "canales más vistos" en
            # el día equivocado, en vez de simplemente faltar ese dato
            # puntual (auditoría, Bajo #22). Ahora se descarta el evento
            # completo, igual que los demás casos de fila malformada.
            logger.warning(
                "Ingesta OTT: timestamp inválido/no parseable (record_id=%s, raw=%r), evento descartado",
                record_id,
                record.get("timestamp"),
            )
            malformed_skipped += 1
            continue

        view_events.append(
            TelemetryOttViewEvent(
                record_id=record_id,
                channel_id=data_id,
                subscriber_code=record.get("subscriberCode"),
                smartcard_id=record.get("smartcardId"),
                device_id=record.get("deviceId"),
                duration_seconds=max(0, record.get("dataDuration") or 0),
                event_date=parsed_timestamp.date(),
                timestamp=parsed_timestamp,
            )
        )

    with transaction.atomic():
        for channel_id, name in channel_names.items():
            TelemetryOttChannelName.objects.update_or_create(
                channel_id=channel_id, defaults={"name": name}
            )

        # OJO: con ignore_conflicts=True, Django no informa cuántas filas
        # realmente se insertaron vs. cuántas chocaron con un record_id ya
        # existente (limitación conocida del ORM) -- esto es la cantidad
        # de eventos *intentados*, no confirmados como nuevos.
        attempted = len(view_events)
        if view_events:
            # `unique_fields=["record_id"]` (auditoría, Medio #11): antes
            # `ignore_conflicts=True` solo, sin esto, le decía a Postgres
            # "ON CONFLICT DO NOTHING" a secas -- silenciaba CUALQUIER
            # violación de constraint, no solo el duplicado esperado por
            # `record_id` (que es la única unique constraint real de este
            # modelo hoy). Con `unique_fields` apuntado, el conflicto
            # ignorado queda acotado a esa columna -- si en el futuro se
            # agrega otra constraint (FK, check) y la viola, vuelve a
            # explotar como error real en vez de desaparecer en silencio.
            TelemetryOttViewEvent.objects.bulk_create(
                view_events, ignore_conflicts=True, unique_fields=["record_id"], batch_size=500
            )

        if max_record_id_seen:
            TelemetryIngestCursor.objects.filter(source=INGEST_SOURCE).update(
                last_record_id=max_record_id_seen
            )

    result = {
        "fetched": len(records),
        "channel_names_upserted": len(channel_names),
        "view_events_attempted": attempted,
        "malformed_skipped": malformed_skipped,
    }
    logger.info("Ingesta OTT completada: %s", result)
    return result
