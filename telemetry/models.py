"""
Modelos de telemetría -- alcance actual: solo eventos OTT (streaming vía
app/web, actionId 7 y 8 en la telemetría de PanAccess). DVB/Catchup/VOD
quedan fuera por ahora (ver conversación de alcance).

Semántica de PanAccess (heredada del proyecto Telemetría original, ver
C:\\Users\\Leonard\\Desktop\\Telemetria\\backend\\delancert\\server\\merge7_8.py):
- actionId=7 = inicio de reproducción OTT. Trae `dataId` (id numérico del
  canal) y `dataName` (nombre del canal).
- actionId=8 = fin de reproducción OTT. Trae `dataDuration` (segundos
  vistos) pero su `dataName` puede venir vacío o desactualizado -- el
  nombre confiable del canal para un `dataId` dado es el más reciente
  visto en un evento actionId=7.

Optimización respecto al proyecto original:
- El proyecto original guardaba TODOS los eventos crudos (7 y 8, y todos
  los demás actionId) en una tabla firehose, y además una copia fusionada
  completa en `MergedTelemetricOTTDelancer` -- duplicando el volumen.
  Aquí en cambio: los eventos actionId=7 nunca se persisten fila por
  fila, solo alimentan una tabla chica de "último nombre conocido por
  canal" (`TelemetryOttChannelName`, una fila por canal, no por evento).
  Solo se persiste el evento "de verdad" (actionId=8, que es el que
  representa una sesión de reproducción completa con su duración) en
  `TelemetryOttViewEvent`. Esto evita duplicar el volumen de eventos sin
  perder la capacidad de resolver el nombre del canal.
- Cursor de ingesta dedicado (`TelemetryIngestCursor`) en vez de calcular
  `Max(recordId)` sobre la tabla de destino en cada corrida -- más barato
  y no se rompe si alguna vez se poda/archiva la tabla de eventos.
"""
from django.db import models


class TelemetryOttChannelName(models.Model):
    """
    Último nombre conocido por canal (por `dataId` de PanAccess).

    Se actualiza (upsert) cada vez que llega un evento actionId=7 durante
    la ingesta -- no se guarda un historial de eventos de inicio, solo el
    nombre más reciente por canal.
    """

    channel_id = models.PositiveIntegerField(
        primary_key=True,
        help_text="dataId de PanAccess -- identificador numérico del canal.",
    )
    name = models.CharField(max_length=200)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "telemetry_ott_channel_name"

    def __str__(self):
        return f"{self.channel_id} -> {self.name}"


class TelemetryOttViewEvent(models.Model):
    """
    Una sesión de reproducción OTT completada (evento actionId=8 de
    PanAccess, resuelto con el nombre de canal más reciente disponible).

    Es la tabla "cruda" de esta app -- alimenta directamente los
    agregados diarios. No se persisten los eventos actionId=7 por
    separado (ver TelemetryOttChannelName).
    """

    record_id = models.BigIntegerField(
        unique=True,
        help_text="recordId de PanAccess -- único, permite ingesta idempotente (ignore_conflicts).",
    )
    channel_id = models.PositiveIntegerField(db_index=True)

    subscriber_code = models.CharField(max_length=50, db_index=True, null=True, blank=True)
    smartcard_id = models.CharField(max_length=50, null=True, blank=True)
    device_id = models.IntegerField(null=True, blank=True)

    duration_seconds = models.PositiveIntegerField(default=0)

    event_date = models.DateField(db_index=True)
    timestamp = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "telemetry_ott_view_event"
        indexes = [
            # Ranking global: "canales más vistos" en una ventana de días.
            models.Index(fields=["event_date", "channel_id"]),
            # Ranking personalizado por suscriptor (fase futura).
            models.Index(fields=["event_date", "subscriber_code", "channel_id"]),
        ]

    def __str__(self):
        return f"record {self.record_id} - canal {self.channel_id} ({self.duration_seconds}s)"


class TelemetryChannelDailyAgg(models.Model):
    """
    Agregado diario por canal -- fuente del ranking GLOBAL de "canales
    más vistos". Se recalcula/actualiza vía tarea periódica a partir de
    TelemetryOttViewEvent; el endpoint de ranking lee de aquí, nunca de
    la tabla cruda directamente.
    """

    day = models.DateField(db_index=True)
    channel_id = models.PositiveIntegerField(db_index=True)

    views = models.PositiveIntegerField(default=0)
    unique_subscribers = models.PositiveIntegerField(default=0)
    total_duration_seconds = models.BigIntegerField(default=0)

    class Meta:
        db_table = "telemetry_channel_daily_agg"
        unique_together = (("day", "channel_id"),)
        indexes = [
            models.Index(fields=["day", "channel_id"]),
            models.Index(fields=["channel_id", "day"]),
        ]


class TelemetryUserChannelDailyAgg(models.Model):
    """
    Agregado diario por (suscriptor, canal) -- fuente del ranking
    PERSONALIZADO ("canales más vistos por ti"), fase futura. Se agrega
    en el mismo paso que TelemetryChannelDailyAgg (un solo recorrido de
    TelemetryOttViewEvent produce ambos), así que no cuesta mucho más
    tenerla lista ahora aunque el ranking global se use primero.
    """

    day = models.DateField(db_index=True)
    subscriber_code = models.CharField(max_length=50, db_index=True)
    channel_id = models.PositiveIntegerField(db_index=True)

    views = models.PositiveIntegerField(default=0)
    total_duration_seconds = models.BigIntegerField(default=0)

    class Meta:
        db_table = "telemetry_user_channel_daily_agg"
        unique_together = (("day", "subscriber_code", "channel_id"),)
        indexes = [
            models.Index(fields=["day", "subscriber_code", "channel_id"]),
            models.Index(fields=["subscriber_code", "channel_id", "day"]),
        ]


class TelemetryIngestCursor(models.Model):
    """
    Cursor de ingesta -- una fila por fuente (hoy solo "ott"). Evita
    recalcular Max(record_id) sobre la tabla de eventos en cada corrida.
    """

    source = models.CharField(max_length=50, unique=True, default="ott")
    last_record_id = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "telemetry_ingest_cursor"

    def __str__(self):
        return f"{self.source}: {self.last_record_id}"
