"""
Sesión PanAccess compartida entre workers vía Redis.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

SESSION_KEY = "panaccess:session_id"
SESSION_LOCK_KEY = "panaccess:session:refresh"


def is_enabled() -> bool:
    return bool(getattr(settings, "PANACCESS_SESSION_USE_REDIS", False))


def _redis_client():
    from appConfig import RedisConfig

    return RedisConfig.get_client()


def get_session_id() -> str | None:
    if not is_enabled():
        return None
    try:
        raw = _redis_client().get(SESSION_KEY)
        if not raw:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except Exception as exc:
        logger.warning("No se pudo leer sesión PanAccess desde Redis: %s", exc)
        return None


def set_session_id(session_id: str, *, ttl_seconds: int | None = None) -> None:
    if not is_enabled() or not session_id:
        return
    ttl = ttl_seconds if ttl_seconds is not None else int(
        getattr(settings, "PANACCESS_SESSION_TTL_SECONDS", 1500)
    )
    try:
        _redis_client().set(SESSION_KEY, session_id, ex=ttl)
    except Exception as exc:
        logger.warning("No se pudo guardar sesión PanAccess en Redis: %s", exc)


def clear_session_id() -> None:
    if not is_enabled():
        return
    try:
        _redis_client().delete(SESSION_KEY)
    except Exception as exc:
        logger.warning("No se pudo borrar sesión PanAccess en Redis: %s", exc)


def refresh_lock():
    """Lock distribuido para un solo login PanAccess entre workers."""
    from appConfig import RedisConfig

    return RedisConfig.task_lock(SESSION_LOCK_KEY, timeout=120)
