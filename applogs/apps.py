from django.apps import AppConfig


class ApplogsConfig(AppConfig):
    """
    Diagnóstico para desarrolladores: errores/crashes reportados por las
    apps cliente (appVideo web/TV, iOS, Android a futuro) y por el propio
    backend, agrupados por "issue" (mismo error repetido) con contexto
    (breadcrumbs, dispositivo, suscriptor si aplica) para que el equipo
    pueda revisar un problema sin depender de que el cliente lo reporte.

    A propósito NO es telemetría de negocio (eso ya vive en la app
    `telemetry`, consumo/reproducción) ni auditoría de seguridad (eso vive
    en `wind.models.AuthAuditLog` / `wind.utils.log_buffer`) -- ver
    docs/LOGS_DIAGNOSTICO_2026-09-01.md.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "applogs"
    verbose_name = "Logs de diagnóstico"
