# Optimización de latencia de login manual (10-15s reportados)

Fecha: 2026-08-26
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Alto #5
Estado: **Implementado, verificado localmente (py_compile / manage.py check / makemigrations --check). Pendiente aplicar la migración y medir tiempos reales en producción.**

## De qué se trata

El cliente reportó una demora de ~10-15 segundos al loguearse en el portal/app. El login manual pasa por `authenticate_portal_user()` (`wind/services/subscriber_auth.py`), que en el peor caso hace varias consultas a Postgres por distintos campos (`code`, `login1`, `login2`, `email`) antes de resolver el usuario. Se investigaron dos causas concretas (no relacionadas con PanAccess, que se cubre aparte en `docs/OPTIMIZACION_DESCUBRIMIENTO_BLOQUEO_LOGIN_2026-08-26.md`):

1. **Consultas `iexact`/`Upper()` sin índice funcional en Postgres.** Sin un índice sobre `UPPER(columna)`, Postgres no puede usar el índice normal de la columna para un filtro `campo__iexact=...` (ni para `Upper('campo')`) y cae a un *sequential scan* completo de la tabla en cada login.
2. **`resolve_subscriber_code()` se llamaba dos o tres veces en el mismo request**, repitiendo el mismo trabajo de resolución de código de suscriptor sin necesidad.

## Qué se investigó primero

Un chequeo inicial reportó "no existe ningún índice funcional" sobre `code`/`emails`. Se verificó ese hallazgo releyendo `wind/models.py` y la migración `0004_db_performance_indexes.py` directamente: esa migración **ya** creaba un índice funcional `Upper(emails)` sobre `ListOfSubscriber` (`wind_lof_sub_emails_upper`), aplicado en producción. El hallazgo real, más acotado, es que a esa migración le faltaron tres columnas que también se consultan por `iexact` en el camino de login:

- `ListOfSubscriber.code` (usado en `resolve_subscriber_code`, vía `code__iexact`).
- `SubscriberEmailRegistry.email` (usado en `resolve_subscriber_code` y en `ensure_subscriber_portal_email_verified`, vía `email__iexact`).
- `SubscriberLoginInfo.login2` (usado en `find_login_record`, vía `login2__iexact`).

## Qué se implementó

### 1. Índices funcionales faltantes (`wind/models.py` + migración `0009_login_functional_indexes.py`)

```python
# ListOfSubscriber.Meta.indexes -- se agregó, junto al Upper(emails) ya existente:
models.Index(Upper('code'), name='wind_lof_sub_code_upper')

# SubscriberLoginInfo -- no tenía Meta, se agregó:
class Meta:
    indexes = [models.Index(Upper('login2'), name='wind_sli_login2_upper')]

# SubscriberEmailRegistry.Meta.indexes -- se agregó, junto a los índices planos ya existentes:
models.Index(Upper('email'), name='wind_ser_email_upper')
```

La migración `0009_login_functional_indexes.py` (depende de `0008_devicesession`) agrega los 3 `AddIndex` correspondientes. Se generó con `manage.py makemigrations wind --name login_functional_indexes` y se verificó que no quedan cambios de modelo sin migrar (`makemigrations --check --dry-run` → "No changes detected").

**Nota de producción:** `CREATE INDEX` normal toma un lock que bloquea escrituras sobre la tabla mientras corre. En tablas grandes esto puede notarse. Si el volumen de `SubscriberLoginInfo`/`SubscriberEmailRegistry`/`ListOfSubscriber` lo amerita, se puede aplicar la migración en una ventana de bajo tráfico, o convertir estos 3 `AddIndex` a `CREATE INDEX CONCURRENTLY` (requiere `atomic = False` en la migración y correrla fuera de una transacción). Se deja como recomendación -- no se cambió el modo de la migración porque no hay visibilidad desde acá del tamaño real de esas tablas en producción.

### 2. Deduplicación de `resolve_subscriber_code()` (`wind/services/subscriber_auth.py`)

- `ensure_subscriber_portal_email_verified(user, login, *, subscriber_code=None)`: ahora acepta el código ya resuelto por el caller. Si no se pasa, resuelve igual que antes (compatibilidad).
- `authenticate_portal_user()` (ahora dividida en un wrapper delgado + `_authenticate_portal_user_core`, ver `docs/OPTIMIZACION_DESCUBRIMIENTO_BLOQUEO_LOGIN_2026-08-26.md` para el porqué de esa división): en las 3 ramas de éxito (usuario Django directo, usuario Django por email, credenciales PanAccess) se resuelve `subscriber_code` **una sola vez** y se reutiliza tanto para `is_subscriber_closed_locally()` como para `ensure_subscriber_portal_email_verified()`. Antes, la rama de credenciales PanAccess resolvía el código hasta 3 veces en el mismo request (una en `verify_panaccess_credentials` internamente, otra explícita antes de este cambio, y una tercera dentro de `ensure_subscriber_portal_email_verified`); ahora reutiliza `login_record.subscriberCode`, que ya viene resuelto.

## Qué queda fuera de este cambio

- No se tocó el pool de conexiones a Postgres (`CONN_MAX_AGE`) ni el número de queries del middleware de autenticación de DRF/allauth -- quedó fuera de alcance porque no hay evidencia (sin acceso a métricas de producción) de que sean parte del problema reportado.
- La otra fuente de latencia identificada -- el descubrimiento por `login1` contra PanAccess (hasta 40 llamadas en el peor caso) -- se resuelve en `docs/OPTIMIZACION_DESCUBRIMIENTO_BLOQUEO_LOGIN_2026-08-26.md` (Fase 2), no en este documento.

## Verificación hecha

- `python3 -m py_compile` sobre `wind/models.py`, `wind/migrations/0009_login_functional_indexes.py`, `wind/services/subscriber_auth.py` -- sin errores.
- `python3 manage.py check` -- "System check identified no issues".
- `python3 manage.py makemigrations wind --check --dry-run` -- "No changes detected in app 'wind'" (confirma que el modelo y la migración están sincronizados).
- **Pendiente (requiere la base de datos real):** aplicar `manage.py migrate` en producción y medir el tiempo de un login antes/después, idealmente con `EXPLAIN ANALYZE` sobre las queries `code__iexact`/`login2__iexact`/`email__iexact` para confirmar que ahora usan el índice funcional en vez de un *sequential scan*.

## Cómo desplegar y verificar en producción

1. `git pull` en `/opt/panaccess-wind`.
2. `python3 manage.py migrate wind` -- aplica la migración `0009`. Si las tablas son grandes y se quiere evitar el lock de escritura, avisar antes de correrlo para evaluar la alternativa `CONCURRENTLY`.
3. Reiniciar los 8 procesos Daphne (`systemctl restart panaccess-wind@800{0..7}.service` o el comando equivalente que ya se usa) para que tomen el código nuevo de `subscriber_auth.py`.
4. Medir: loguearse un par de veces con una cuenta real y cronometrar, comparando contra los 10-15s reportados. Si se quiere confirmar a nivel de query, correr en `manage.py dbshell`:
   ```sql
   EXPLAIN ANALYZE SELECT * FROM wind_listofsubscriber WHERE UPPER(code) = UPPER('ALGUN_CODIGO');
   ```
   y confirmar que el plan usa `Index Scan` sobre `wind_lof_sub_code_upper` en vez de `Seq Scan`.
