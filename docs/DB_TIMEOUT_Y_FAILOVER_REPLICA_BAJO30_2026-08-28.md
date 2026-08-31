# Timeout de conexión + circuit breaker de réplica — Bajo #30

Fecha: 2026-08-28

## Qué

Hallazgo Bajo #30: "Router de réplica de base de datos sin failover; falta un timeout de conexión a DB explícito." Dos piezas, ambas ya implementadas y activas independientemente de si la réplica está configurada o no:

### 1. `connect_timeout` explícito

`appConfig.DatabaseConfig.CONNECT_TIMEOUT_SECONDS` (nuevo, `DB_CONNECT_TIMEOUT_SECONDS` en `.env`, default 10s) se agrega a `OPTIONS` en `django_default_database()`. Como `django_replica_database()` parte de `django_default_database()` (`base = cls.django_default_database()`), la réplica lo hereda automáticamente sin código adicional. Antes, un host de Postgres caído o inalcanzable (primaria o réplica) se colgaba con el timeout de socket por defecto del SO/red -- potencialmente minutos -- en vez de fallar rápido.

### 2. Circuit breaker de salud para la réplica

Nuevo en `wind/db_router.py`:
- `mark_replica_unhealthy(ttl_seconds=...)` / `mark_replica_healthy()` / `is_replica_healthy()` -- una marca simple en el caché de Django (`db_router:replica_unhealthy`).
- `PrimaryReplicaRouter.db_for_read()` ahora consulta `is_replica_healthy()` antes de devolver `'replica'` -- si está marcada "no saludable", cae a `'default'` (primaria), igual que ya hacía con `use_primary_for_reads()`.

Nuevo en `wind/tasks.py`: `check_replica_health_task` -- un `SELECT 1` barato contra la conexión `'replica'` cada `DB_REPLICA_HEALTHCHECK_MINUTES` (default 2, vía Celery beat). Si falla, marca "no saludable" por `DB_REPLICA_UNHEALTHY_TTL_SECONDS` (default 300s); si responde bien, limpia la marca (recuperación inmediata, no hay que esperar a que expire el TTL). Solo se registra en `CELERY_BEAT_SCHEDULE` si `_replica` está configurado (`DB_REPLICA_HOST` no vacío) **y** `DB_REPLICA_HEALTHCHECK_ENABLED=true` -- en el estado actual de producción (`DB_REPLICA_HOST` vacío), esta tarea no se registra ni corre.

Nuevo en `appConfig.CeleryConfig`: `DB_REPLICA_HEALTHCHECK_ENABLED`, `DB_REPLICA_HEALTHCHECK_MINUTES`, `DB_REPLICA_UNHEALTHY_TTL_SECONDS`.

Corregido de paso en `panaccess_wind_integration/settings.py`: `_replica` ahora se inicializa en `None` antes del `if DatabaseConfig.use_postgresql():` -- antes, si el proyecto corría contra SQLite (rama `else` de ese bloque), la variable `_replica` nunca se definía, y la nueva verificación `if _replica and ...` para el beat schedule habría lanzado `NameError` en ese caso. No afecta al despliegue real (que sí usa Postgres), pero es la forma correcta de escribirlo.

## Por qué

Con `DB_REPLICA_HOST` vacío hoy, la réplica no está en uso todavía -- este cambio no altera el comportamiento actual en producción. Pero cuando el cliente decida activarla, dos riesgos quedan cerrados de entrada en vez de descubrirse en el primer incidente real: (a) sin `connect_timeout`, una réplica caída no solo deja de servir lecturas -- cuelga cada request que intente usarla; (b) sin el circuit breaker, cada request individual pagaría ese timeout de conexión contra una réplica caída hasta que alguien note el problema y apague el router a mano. Con el chequeo periódico, el sistema se auto-degrada a la primaria en como máximo `DB_REPLICA_HEALTHCHECK_MINUTES` desde que la réplica cae, y se recupera solo en el siguiente chequeo exitoso.

## Cómo se verificó

- `python3 -m py_compile` + `pyflakes` sobre los 4 archivos tocados -- limpio.
- `python3 manage.py check` -- `System check identified no issues (0 silenced)`.
- `wind/tests/test_replica_health.py` (11 tests nuevos, corridos contra Postgres real en sandbox): `connect_timeout` presente en la config por defecto y heredado por la réplica cuando está configurada; el circuit breaker marca/desmarca correctamente y expira solo; el router cae a primaria cuando la réplica está marcada no saludable pero respeta `use_primary_for_reads()` igual que antes; la tarea marca no-saludable si la query falla, saludable si responde, y es no-op si la bandera está apagada.

## Archivos tocados

- `appConfig.py` (`DatabaseConfig.CONNECT_TIMEOUT_SECONDS` + wiring; `CeleryConfig.DB_REPLICA_HEALTHCHECK_*`)
- `wind/db_router.py` (`mark_replica_unhealthy`/`mark_replica_healthy`/`is_replica_healthy`, `db_for_read` actualizado)
- `wind/tasks.py` (`check_replica_health_task`)
- `panaccess_wind_integration/settings.py` (`_replica = None` antes del if/else; nueva entrada en `CELERY_BEAT_SCHEDULE` gateada por `_replica`)
- `.env` (`DB_CONNECT_TIMEOUT_SECONDS`, `CELERY_DB_REPLICA_HEALTHCHECK_ENABLED`, `DB_REPLICA_HEALTHCHECK_MINUTES`, `DB_REPLICA_UNHEALTHY_TTL_SECONDS`)
- `wind/tests/test_replica_health.py` (nuevo)
- `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (fila Bajo #30 actualizada)
