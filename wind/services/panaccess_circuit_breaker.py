"""
Circuit breaker para llamadas PanAccess.

Estado compartido en Redis (antes vivía en memoria del propio proceso, así
que cada worker tenía su propio circuito y podían no coincidir -- uno
"abierto" protegiendo mientras los demás seguían golpeando a PanAccess sin
saberlo). Ahora todos los workers leen/escriben las mismas llaves en Redis.
"""
from __future__ import annotations

import logging
import threading

from django.conf import settings

from wind.exceptions import (
    PanAccessAuthenticationError,
    PanAccessConnectionError,
    PanAccessException,
    PanAccessRateLimitError,
    PanAccessSessionError,
    PanAccessTimeoutError,
)

logger = logging.getLogger(__name__)

_OPEN_KEY = "panaccess:cb:open"
_FAILURES_KEY = "panaccess:cb:failures"

# Excepciones que cuentan como "PanAccess no está bien" para el circuito.
#
# OJO: PanAccessAPIError (y PanAccessException genérico) se dejan afuera a
# propósito. El cliente (panaccess_client.py) también los usa para errores
# de negocio comunes con respuesta HTTP 200 (ej. "el email ya existe" al
# registrar), que no tienen nada que ver con la salud de PanAccess --
# contarlos abriría el circuito para TODOS los usuarios solo porque varios
# registros seguidos fallaron por validación de otro usuario. Sí se cuentan
# PanAccessSessionError/PanAccessRateLimitError/PanAccessAuthenticationError
# porque esas sí son señales reales de que PanAccess (o la sesión/cuenta de
# servicio) no está respondiendo bien.
COUNTED_FAILURE_TYPES = (
    PanAccessConnectionError,
    PanAccessTimeoutError,
    PanAccessSessionError,
    PanAccessRateLimitError,
    PanAccessAuthenticationError,
)


def _redis_client():
    from appConfig import RedisConfig

    return RedisConfig.get_client()


class PanAccessCircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

    def _is_open(self) -> bool:
        try:
            return bool(_redis_client().get(_OPEN_KEY))
        except Exception:
            logger.warning(
                "No se pudo leer el estado del circuit breaker en Redis; se asume cerrado",
                exc_info=True,
            )
            return False

    def _record_failure(self) -> None:
        try:
            client = _redis_client()
            failures = client.incr(_FAILURES_KEY)
            # Ventana de conteo: si nadie vuelve a fallar en 10x el tiempo de
            # recuperación, el contador se limpia solo (evita que fallos muy
            # viejos y sueltos, sin relación entre sí, terminen sumando).
            client.expire(_FAILURES_KEY, self.recovery_timeout * 10)
            if failures >= self.failure_threshold:
                client.set(_OPEN_KEY, "1", ex=self.recovery_timeout)
                client.delete(_FAILURES_KEY)
                logger.error(
                    "Circuit breaker PanAccess ABIERTO tras %s fallos (recovery=%ss)",
                    failures,
                    self.recovery_timeout,
                )
        except Exception:
            logger.warning(
                "No se pudo registrar el fallo del circuit breaker en Redis",
                exc_info=True,
            )

    def _record_success(self) -> None:
        try:
            _redis_client().delete(_FAILURES_KEY)
        except Exception:
            logger.warning(
                "No se pudo limpiar el contador del circuit breaker en Redis",
                exc_info=True,
            )

    def execute(self, fn):
        if self._is_open():
            raise PanAccessException(
                "PanAccess temporalmente no disponible (circuit breaker abierto). "
                f"Reintenta en {self.recovery_timeout}s."
            )

        try:
            result = fn()
        except COUNTED_FAILURE_TYPES as exc:
            self._record_failure()
            raise exc
        except Exception:
            raise
        else:
            self._record_success()
            return result


_breaker: PanAccessCircuitBreaker | None = None
_breaker_lock = threading.Lock()


def get_circuit_breaker() -> PanAccessCircuitBreaker:
    global _breaker
    if _breaker is None:
        with _breaker_lock:
            if _breaker is None:
                _breaker = PanAccessCircuitBreaker(
                    failure_threshold=int(
                        getattr(settings, "PANACCESS_CB_FAILURE_THRESHOLD", 5)
                    ),
                    recovery_timeout=int(
                        getattr(settings, "PANACCESS_CB_RECOVERY_SECONDS", 60)
                    ),
                )
    return _breaker


def circuit_breaker_enabled() -> bool:
    return bool(getattr(settings, "PANACCESS_CIRCUIT_BREAKER_ENABLED", False))
