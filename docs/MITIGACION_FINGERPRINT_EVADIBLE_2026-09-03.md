# Mitigación: fingerprint de dispositivo evadible (hallazgo #16)

Fecha: 2026-09-03
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (hallazgo #16), `docs/GUIA_INTEGRACION_UNIFICADA.md` (sección 1.1, contrato de `request-udid-manual`).

## Qué

`GET /wind/request-udid-manual/` (la TV pidiendo un código de pareo) tenía un solo rate limit: 1 cada 5 minutos por `device_fingerprint`. Ese fingerprint se deriva 100% server-side de headers de la request (`User-Agent`, `Accept-Language`, `Accept-Encoding`, etc. -- ver `generate_device_fingerprint` en `wind/utils/websocket_utils.py`), pero nada impide que un cliente insistente rote esos headers en cada intento y obtenga un fingerprint "nuevo" cada vez, esquivando por completo ese límite.

Se agregó una **segunda capa de rate limit, por IP**, que se suma a la existente sin reemplazarla:

- `check_udid_request_ip_rate_limit(client_ip, max_requests=10, window_minutes=5)` (`wind/utils/websocket_utils.py`), mismo patrón de reserva atómica (`_reserve_atomic_slot`) que ya usan `check_device_fingerprint_rate_limit`, `check_udid_rate_limit`, etc.
- `RequestUDIDManualView.get()` (`wind/views.py`) la chequea primero, antes de calcular el fingerprint -- si alguien ya viene rotando headers para esquivar el límite de abajo, se corta acá sin necesidad de llegar a esa segunda revisión. Nuevo `error_code: "IP_RATE_LIMIT_EXCEEDED"` (mismo shape de respuesta 429 que el límite existente: `retry_after`, `retry_at`, `remaining_requests`).
- El límite por fingerprint (`DEVICE_FP_RATE_LIMIT_EXCEEDED`, 1/5min) sigue exactamente igual que antes, sin ningún cambio de comportamiento para quien no está abusando.

## Por qué

**Por qué 10/5min y no algo más estricto:** una IP real puede estar compartida por varios dispositivos legítimos detrás del mismo NAT -- varias TVs de una casa u oficina pidiendo su propio código de pareo al mismo tiempo no deberían chocar contra este límite. 10 en 5 minutos deja margen amplio para ese caso normal, mientras sigue cortando a alguien que dispara docenas de requests seguidas rotando headers.

**Por qué no reemplaza al fingerprint:** el límite por fingerprint sigue siendo la primera línea de defensa para el caso común (un mismo dispositivo pidiendo de más sin ninguna intención de evadir nada). El límite por IP es específicamente la respuesta al caso adversarial (alguien rotando headers a propósito) -- son complementarios, no alternativos.

**Por qué esto es "mitigado" y no "resuelto" del todo:** la IP sigue siendo evadible con suficiente esfuerzo (rotar de red, usar varias IPs/proxies) -- no es una solución dura tipo attestation de hardware (que tampoco es viable de forma uniforme entre plataformas de Smart TV, como ya señalaba la auditoría). Es una mitigación de bajo esfuerzo que sube el costo real de abusar del endpoint sin cambiar el contrato para nadie más.

## Cómo se verificó

- `python3 -m py_compile` limpio sobre `wind/views.py` y `wind/utils/websocket_utils.py`.
- `manage.py check` -- sin problemas, corrido contra Postgres real (`pgserver`).
- `wind/tests/test_udid_request_ip_rate_limit.py` (6 tests, corridos contra Postgres real):
  - `check_udid_request_ip_rate_limit` en aislamiento: sin IP no permite, permite hasta el máximo configurado, bloquea al excederlo con el `retry_after` correcto, e IPs distintas quedan aisladas entre sí (no comparten cupo).
  - Contra la vista real (`RequestUDIDManualView`): 10 requests con `User-Agent` distinto cada vez (fingerprint distinto en cada una, simulando el ataque real) desde la misma IP pasan, la 11ª se corta con `IP_RATE_LIMIT_EXCEEDED` -- confirma que la segunda capa sí corta el escenario que el fingerprint por sí solo no cubre. Un segundo test confirma que el límite por fingerprint (`DEVICE_FP_RATE_LIMIT_EXCEEDED`) sigue funcionando igual que antes cuando no hay rotación de headers.
  - **6/6 OK.**
- Se mockeó el cliente Redis (`wind.utils.log_buffer._get_redis_client`) solo en los dos tests que hacen varios requests seguidos -- sin Redis disponible en el entorno de test, cada llamada a `log_audit_async` agregaba varios segundos de latencia real por el intento de conexión fallido. No afecta la lógica bajo prueba (rate limiting), solo evita ruido/lentitud ajena al test.

## Archivos tocados

- `wind/utils/websocket_utils.py` (`check_udid_request_ip_rate_limit`, nueva función)
- `wind/views.py` (`RequestUDIDManualView.get()`, import y chequeo nuevo)
- `wind/tests/test_udid_request_ip_rate_limit.py` (nuevo, 6 tests)
- `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (hallazgo #16 actualizado)

## Qué queda afuera

- El fingerprint por headers sigue siendo evadible en sí mismo -- esta mitigación no lo endurece, solo agrega una defensa adicional independiente.
- No se tocó ningún otro endpoint (`validate-and-associate-udid`, `associate-udid-by-account`, WebSockets de pareo) -- todos tienen sus propios límites ya existentes, sin relación con este fingerprint puntual.
- El nuevo `error_code: "IP_RATE_LIMIT_EXCEEDED"` ya quedó documentado en la sección 1.1 de `docs/GUIA_INTEGRACION_UNIFICADA.md` -- es opcional para el equipo de TV distinguirlo del existente: el manejo recomendado ("pedí un código nuevo, respetá `retry_after`") es el mismo para cualquiera de los dos.
