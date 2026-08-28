# Telemetría OTT: logging de filas descartadas + acotar ignore_conflicts (Medio #10, #11, #12)

Fecha: 2026-08-28
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgos Medio #10, #11 y #12.

## Medio #10 -- filas OTT corruptas descartadas en silencio

`ingest_new_ott_records()` (`telemetry/services/panaccess_ott_ingest.py`) tenía dos puntos donde una fila se descartaba con un `continue` mudo, sin ningún `logger.warning`:

1. Un `actionId` que no es ni 7 (START) ni 8 (STOP) -- el comentario ya decía "no debería pasar", pero no había forma de enterarse si empezaba a pasar.
2. Un evento STOP (`actionId=8`) al que le falta `dataId` o `recordId` -- sin esos dos campos no se puede armar el `TelemetryOttViewEvent`, así que se descartaba, pero tampoco quedaba registro de que había pasado.

Ahora ambos casos loguean `logger.warning` con los datos disponibles (`action_id`/`record_id`, o cuáles de `data_id`/`record_id` faltan), y se cuentan en una variable `malformed_skipped` que se agrega al diccionario de resultado de la función (`result["malformed_skipped"]`) -- visible en el log final `"Ingesta OTT completada: %s"` de cada corrida, sin tener que ir a buscar warnings sueltos.

## Medio #11 -- `bulk_create(ignore_conflicts=True)` acotado a `record_id`

`TelemetryOttViewEvent.record_id` ya tiene `unique=True` a nivel de modelo (`telemetry/models.py:66-69`) -- es la única constraint real que puede chocar hoy, y es la que permite la ingesta idempotente (reintentar el mismo lote sin duplicar). El problema: `bulk_create(view_events, ignore_conflicts=True, batch_size=500)` sin más, en Postgres, arma un `ON CONFLICT DO NOTHING` a secas -- silencia **cualquier** violación de constraint, no solo la de `record_id`. Hoy no hay ninguna otra constraint en el modelo, así que el riesgo real es bajo, pero si se agrega una FK o un check constraint en el futuro, este patrón escondería una pérdida de datos real sin ningún error visible.

Se agregó `unique_fields=["record_id"]` a la llamada. Con eso, Django le dice a Postgres `ON CONFLICT (record_id) DO NOTHING` en vez de `ON CONFLICT DO NOTHING` -- el conflicto ignorado queda acotado a esa columna específica. Si en el futuro se viola alguna otra constraint, vuelve a fallar con un error real en vez de desaparecer en silencio.

## Medio #12 -- guardarraíl en `TopChannelsGlobalView`

No requería cambio de comportamiento -- `IsAuthenticated` sigue siendo correcto hoy porque el ranking de "canales más vistos" es global (mismo resultado para cualquier suscriptor). Se agregó un comentario explícito en la vista (`telemetry/views.py`) dejando dicho que si este endpoint alguna vez empieza a devolver datos filtrados por suscriptor/región/cuenta, hay que agregar ahí el chequeo de autorización correspondiente -- guardarraíl para quien lo toque en el futuro, sin asumir que `IsAuthenticated` sigue alcanzando solo porque alcanzaba para el caso global.

## Verificación realizada

- `py_compile` sobre `telemetry/services/panaccess_ott_ingest.py` y `telemetry/views.py`: sin errores.
- `manage.py check`: `System check identified no issues (0 silenced)`.
- Sin cambios de esquema/migración -- `unique_fields` usa la constraint que ya existía; el logging es puramente aditivo.
- Pendiente (no bloqueante): correr una ingesta real en producción y confirmar que `malformed_skipped` aparece en el log de resumen (debería dar 0 en operación normal, ya que hoy PanAccess no manda filas corruptas conocidas).

## Estado

Medio #10, #11 y #12 quedan **resueltos**.
