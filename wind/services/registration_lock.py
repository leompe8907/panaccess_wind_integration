"""
Lock distribuido para el alta de suscriptores, con llave por email (y por
documento si viene). Cierra dos huecos de condición de carrera detectados
en la auditoría (ver docs/AUDITORIA_DECISIONES_Y_PENDIENTES.md, sección 16):

  - Doble alta de suscriptor con el mismo email/documento: dos requests
    concurrentes podían pasar ambas la validación de "no existe todavía"
    antes de que cualquiera escribiera el registro, y cada una crear su
    propio suscriptor en PanAccess.
  - Doble concesión del producto de prueba (trial): mismo patrón entre
    is_eligible_for_trial() (lectura) y mark_trial_granted() (escritura).

No se implementa con `RedisConfig.task_lock` (que es un context manager)
porque el alta de suscriptor necesita liberar el lock en varios puntos de
salida distintos (éxito síncrono, éxito en modo async, y cada rama de
error) sin reindentar las ~250 líneas de `create_subscriber_view` -- acá se
maneja el acquire/release a mano, con el mismo objeto `redis.lock.Lock` que
usa `task_lock` por debajo.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

REGISTRATION_LOCK_TIMEOUT = 60   # TTL de respaldo si algo se cuelga sin liberar
REGISTRATION_LOCK_WAIT = 8       # cuánto espera una segunda request antes de rendirse


def _lock_keys(email_normalized: str, document: str | None) -> list[str]:
    keys = [f"register:email:{(email_normalized or '').strip().lower()}"]
    doc = (document or "").strip().upper()
    if doc:
        keys.append(f"register:doc:{doc}")
    return keys


def acquire_registration_locks(email_normalized: str, document: str | None = None):
    """
    Adquiere (bloqueante, con espera corta) los locks de email y, si aplica,
    de documento -- siempre en el mismo orden para no generar deadlocks
    entre dos registros que compartieran ambas llaves cruzadas.

    Devuelve la lista de locks adquiridos si tuvo éxito con todos, o None si
    no pudo con alguno (y libera los que sí alcanzó a tomar).
    """
    from appConfig import RedisConfig
    from redis.lock import Lock

    keys = _lock_keys(email_normalized, document)
    client = RedisConfig.get_client()
    acquired_locks = []

    for key in keys:
        lock = Lock(client, key, timeout=REGISTRATION_LOCK_TIMEOUT)
        got = lock.acquire(blocking=True, blocking_timeout=REGISTRATION_LOCK_WAIT)
        if not got:
            logger.warning(
                "No se pudo adquirir el lock de registro '%s' -- ya hay otro registro "
                "en curso para el mismo email/documento",
                key,
            )
            release_registration_locks(acquired_locks)
            return None
        acquired_locks.append(lock)

    return acquired_locks


def release_registration_locks(locks) -> None:
    if not locks:
        return
    for lock in locks:
        try:
            lock.release()
        except Exception:
            logger.debug("Lock de registro ya liberado o expirado", exc_info=True)
