# Fingerprint de dispositivo evadible (pareo de TV)

Estado: **mitigado enteramente del lado del backend -- no requiere ninguna acción de ningún cliente, incluido iOS/Android.** Documento informativo.
Referencia: `docs/MITIGACION_FINGERPRINT_EVADIBLE_2026-09-03.md` (detalle completo), `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 1.1.

## Qué es y a quién le aplica

`GET /wind/request-udid-manual/` (pedir un código de pareo) limita 1 pedido cada 5 minutos por `device_fingerprint`. Ese fingerprint se calcula 100% del lado del servidor a partir de headers de la request -- nada impide que un cliente insistente rote esos headers y obtenga un fingerprint "nuevo" cada vez, esquivando el límite.

**Importante: este endpoint lo llama la TV, no el celular ni la app de iOS/Android.** El fingerprint identifica al dispositivo que pide el código de pareo (la TV), no al dispositivo que lo escanea. Ver `06_vincular_dispositivo.md` para el rol de cada plataforma en este flujo.

## Qué se hizo (ya implementado, 2026-09-03)

Se agregó una segunda capa de límite **por IP** (10 pedidos cada 5 minutos), independiente del límite por fingerprint. Si alguien rota headers para evadir el límite por fingerprint, la IP sigue siendo la misma y esta segunda capa lo corta. Nuevo código de error `IP_RATE_LIMIT_EXCEEDED` (mismo shape 429 que ya existía: `retry_after`, `retry_at`, `remaining_requests`).

## Por qué sigue listado como "abierto"

La IP en sí también es evadible con más esfuerzo (rotar de red, proxies) -- no es una solución dura tipo attestation de hardware (inviable de forma uniforme entre plataformas de Smart TV). Es una mitigación de bajo costo que sube la barrera de abuso, no una solución definitiva. Sin una solución de bajo riesgo identificada para cerrarla del todo.

## Qué debe implementar iOS/Android nativo

**Nada.** Este punto es exclusivamente responsabilidad del código de la TV (`GET /wind/request-udid-manual/`). El celular solo consume el resultado (el `udid`/QR ya generado) -- ver `06_vincular_dispositivo.md`. Se documenta acá únicamente para que el equipo mobile sepa que la limitación existe y fue evaluada, no para que implemente ningún cambio.

Si en algún momento se agrega una TV basada en Android TV que reutilice código de la app Android, sí aplicaría -- pero hoy no es el caso.
