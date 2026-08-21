from django.apps import AppConfig


class TelemetryConfig(AppConfig):
    """
    App de telemetría: ingesta periódica de eventos de consumo (PanAccess),
    agregación y exposición de métricas (ej. "canales más vistos") a las
    apps cliente.

    Reutiliza la conexión/sesión a PanAccess que ya vive en
    `wind.services.panaccess_singleton` -- no reimplementa login/sesión
    propios (a diferencia del proyecto Telemetría original, que sí lo
    hacía, ver docs/TELEMETRY_MIGRATION.md una vez exista).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "telemetry"
    verbose_name = "Telemetría"
