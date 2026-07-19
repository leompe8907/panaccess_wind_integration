"""
Router de lecturas/escrituras entre BD primaria y réplica de solo lectura.

Por defecto TODAS las lecturas van a la réplica y las escrituras a la
primaria -- la réplica puede tener lag de replicación (típicamente
milisegundos, pero sin garantía dura) respecto a la primaria. Para flujos
que necesitan leer un dato inmediatamente después de escribirlo en el mismo
request/tarea -- donde una lectura stale tendría consecuencias reales, no
solo cosméticas -- usar `use_primary_for_reads()` alrededor de esas lecturas
puntuales. Fuerza esas queries concretas a 'default' (primaria) sin afectar
el resto del tráfico de lectura del sistema, que sigue yendo a réplica.

Caso concreto ya identificado (auditoría): `is_subscriber_closed_locally()`
en `wind.services.subscriber_auth` decide si un login se rechaza porque el
abonado está cerrado localmente. Si esa lectura fuera a réplica y la
réplica tuviera aunque sea un pequeño lag respecto al cierre recién escrito
en primaria, existiría una ventana real en la que una cuenta recién cerrada
podría seguir autenticando -- exactamente el bypass que ese fix buscaba
cerrar. Por eso esa función fuerza primaria explícitamente (ver abajo).
"""
from __future__ import annotations

import contextvars

_force_primary_reads: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "force_primary_reads", default=False
)


class use_primary_for_reads:
    """
    Context manager: dentro del bloque `with`, `PrimaryReplicaRouter.db_for_read`
    devuelve 'default' (primaria) en vez de 'replica' para cualquier query
    de lectura hecha en ese contexto (incluye llamadas anidadas). Es seguro
    anidarlo.
    """

    def __enter__(self):
        self._token = _force_primary_reads.set(True)
        return self

    def __exit__(self, exc_type, exc, tb):
        _force_primary_reads.reset(self._token)
        return False


class PrimaryReplicaRouter:
    def db_for_read(self, model, **hints):
        if _force_primary_reads.get():
            return 'default'
        return 'replica'

    def db_for_write(self, model, **hints):
        return 'default'

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return True
