"""
Circuit breaker para llamadas PanAccess.
"""
from __future__ import annotations

import logging
import threading
import time

from django.conf import settings

from wind.exceptions import PanAccessConnectionError, PanAccessException, PanAccessTimeoutError

logger = logging.getLogger(__name__)


class PanAccessCircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    def _is_open(self) -> bool:
        if self._failures < self.failure_threshold:
            return False
        if (time.time() - self._opened_at) >= self.recovery_timeout:
            return False
        return True

    def execute(self, fn):
        with self._lock:
            if self._is_open():
                raise PanAccessException(
                    "PanAccess temporalmente no disponible (circuit breaker abierto). "
                    f"Reintenta en {self.recovery_timeout}s."
                )

        try:
            result = fn()
        except (PanAccessConnectionError, PanAccessTimeoutError) as exc:
            with self._lock:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._opened_at = time.time()
                    logger.error(
                        "Circuit breaker PanAccess ABIERTO tras %s fallos",
                        self._failures,
                    )
            raise exc
        except Exception:
            raise
        else:
            with self._lock:
                self._failures = 0
                self._opened_at = 0.0
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
