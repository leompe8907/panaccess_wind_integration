"""
Sesión PanAccess compartida entre workers vía Redis.
"""
from __future__ import annotations

import logging

from django.conf import settings

from wind.utils.encryption import decrypt_value, encrypt_value

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
        stored = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except Exception as exc:
        logger.warning("No se pudo leer sesión PanAccess desde Redis: %s", exc)
        return None

    try:
        return decrypt_value(stored)
    except Exception as exc:
        # Cubre tambien el caso de upgrade: un valor viejo guardado en claro
        # (antes de este cambio) no va a desencriptar -- se trata como
        # cache-miss para forzar un login nuevo, en vez de propagar el
        # sessionId en texto plano.
        logger.warning("SessionId de PanAccess en Redis no se pudo desencriptar, se ignora: %s", exc)
        return None


def set_session_id(session_id: str, *, ttl_seconds: int | None = None) -> None:
    if not is_enabled() or not session_id:
        return
    ttl = ttl_seconds if ttl_seconds is not None else int(
        getattr(settings, "PANACCESS_SESSION_TTL_SECONDS", 1500)
    )
    try:
        _redis_client().set(SESSION_KEY, encrypt_value(session_id), ex=ttl)
    except Exception as exc:
        logger.warning("No se pudo guardar sesión PanAccess en Redis: %s", exc)


def clear_session_id() -> None:
    if not is_enabled():
        return
    try:
        _redis_client().delete(SESSION_KEY)
    except Exception as exc:
        logger.warning("No se pudo borrar sesión PanAccess en Redis: %s", exc)


def refresh_lock(*, blocking: bool = True, blocking_timeout: float = 15.0):
    """Lock distribuido para un solo login PanAccess entre workers.

    Es bloqueante por defecto: un proceso que no consigue el lock espera
    hasta ``blocking_timeout`` segundos a que el que sí lo tiene termine de
    autenticarse y publique el sessionId en Redis, en vez de autenticarse
    también (eso causaba "login storms" contra PanAccess — varios workers
    haciendo login al mismo tiempo y disparando su límite de intentos).
    """
    from appConfig import RedisConfig

    return RedisConfig.task_lock(
        SESSION_LOCK_KEY,
        timeout=120,
        blocking=blocking,
        blocking_timeout=blocking_timeout,
    )
