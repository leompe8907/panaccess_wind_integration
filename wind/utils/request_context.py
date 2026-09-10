"""
Contexto de la request/conexión actual, accesible desde código que no
tiene el objeto `request`/`scope` a mano.

Motivo: `applogs.logging_handler.DiagnosticsLogHandler` (el handler que
manda los ERROR+ del propio backend al mismo pipeline de diagnóstico que
usan las apps cliente, ver docs/LOGS_DIAGNOSTICO_2026-09-01.md) solo
recibe un `logging.LogRecord` plano -- sin ningún vínculo al request que
disparó ese log. Un `logger.error(...)` varias capas por debajo de
cualquier vista (ej. `wind/utils/panaccess_auth.py`, disparado por
CUALQUIER request que en el camino necesite hablar con PanAccess) no
tenía forma de saber qué IP originó esa request, así que
`LogEvent.client_ip` quedaba siempre `NULL` para la plataforma
`backend` (a diferencia del endpoint HTTP de ingesta de apps cliente,
que sí recibe `client_ip` porque tiene el `request` a mano).

Se usa `contextvars.ContextVar` en vez de un threadlocal clásico porque
es seguro tanto en el mundo sync (WSGI, un thread por request) como en
el ASGI async de Channels (cada conexión corre en su propia Task de
asyncio, y los contextvars se copian correctamente por Task -- y
`asgiref.sync_to_async`/`database_sync_to_async` también propagan el
contexto al thread pool que usan por debajo).
"""
from __future__ import annotations

import contextvars

_current_client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_client_ip", default=None
)


def set_current_client_ip(ip: str | None):
    """Guarda la IP para el contexto actual (request HTTP o conexión WS). Devuelve un token para `reset_current_client_ip`."""
    return _current_client_ip.set(ip or None)


def reset_current_client_ip(token) -> None:
    """Restaura el valor anterior. Nunca lanza -- un token inválido/`None` no debe romper nada que esté cerrando una request/conexión."""
    if token is None:
        return
    try:
        _current_client_ip.reset(token)
    except Exception:
        pass


def get_current_client_ip() -> str | None:
    """IP del request/conexión actual, o `None` si no hay ninguna en contexto (ej. una tarea de Celery, sin request de por medio)."""
    return _current_client_ip.get()
