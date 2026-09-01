"""
Handler de logging que envía los ERROR+ del propio backend Django hacia el
mismo pipeline de diagnóstico (`applogs.services.record_log_event`) que usa
la ingesta HTTP de las apps cliente -- ver
docs/LOGS_DIAGNOSTICO_2026-09-01.md. Wireado en `LOGGING` (settings.py)
junto al handler `error_file` ya existente (mismos loggers, mismo nivel).

Reglas duras, porque esto corre dentro del logging normal de cualquier
request/tarea:
- `emit()` NUNCA debe dejar escapar una excepción -- degradaría en cascada
  cualquier código que esté logueando un error real.
- Nunca loguea a través de un logger que tenga este mismo handler adjunto
  (evita recursión) -- usa `_internal_logger`, que nunca se configura acá.
"""
import logging
import traceback

_internal_logger = logging.getLogger("applogs.logging_handler_internal")


class DiagnosticsLogHandler(logging.Handler):
    def emit(self, record):
        try:
            self._emit(record)
        except Exception as exc:  # nunca romper el logging normal
            _internal_logger.warning("DiagnosticsLogHandler: fallo interno: %s", exc)

    def _emit(self, record):
        from appConfig import AppLogsConfig

        if not AppLogsConfig.BACKEND_CAPTURE_ENABLED:
            return

        # Import diferido: LOGGING se termina de configurar mientras
        # settings.py todavía se está cargando -- importar applogs.services
        # (que importa modelos) al nivel de módulo acá rompería el arranque.
        from django.apps import apps

        if not apps.ready:
            return

        from applogs.models import LogIssue
        from applogs.services import record_log_event

        message = record.getMessage()
        stack = ""
        if record.exc_info:
            stack = "".join(traceback.format_exception(*record.exc_info))

        record_log_event(
            platform=LogIssue.PLATFORM_BACKEND,
            level=LogIssue.LEVEL_ERROR if record.levelno >= logging.ERROR else LogIssue.LEVEL_WARNING,
            message=message[:2000],
            stack=stack[:8000],
            extra={"logger": record.name, "path": getattr(record, "pathname", "")},
        )
