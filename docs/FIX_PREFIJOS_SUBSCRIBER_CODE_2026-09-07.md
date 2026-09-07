# Incidente de producción: registro de suscriptores roto desde 2026-09-01

Fecha: 2026-09-07
Severidad: crítica -- registro de suscriptores nuevos (manual y social) completamente roto en producción durante ~6 días.
Detectado vía: alertas por email del sistema de logs de diagnóstico (`applogs`, ver `docs/LOGS_DIAGNOSTICO_2026-09-01.md`) -- spike de 100 ocurrencias en 4 issues relacionados.

## Qué pasó

Los prefijos de `subscriber_code` introducidos en la "Fase de prefijos" (`wind/functions/create_subscriber.py`) usaban el carácter `$`:

```python
MANUAL_CODE_PREFIX = "BM$"
SOCIAL_PROVIDER_CODE_PREFIXES = {"google": "BG$", "facebook": "BF$"}
DEFAULT_SOCIAL_CODE_PREFIX = "BG$"
MANUAL_AUTO_CODE_PREFIX = "BM$AUTO"
```

PanAccess solo acepta valores alfanuméricos (`a-z`, `A-Z`, `0-9`) en su campo `code` -- lo dice el propio mensaje de error que devuelve: *"El valor de 'code' no es un valor alfanumérico válido. (a-z, A-Z, 0-9)"*. Cualquier código con `$` es rechazado siempre, sin excepción.

**Alcance real -- no solo registro manual con documento.** `_create_subscriber_core()` (la función que arma y manda el `code` con prefijo) es compartida por:
- `create_subscriber_view` (registro público manual, con o sin documento).
- `social_login_provisioning.create_subscriber_in_panaccess()` (alta automática de suscriptor nuevo la primera vez que alguien hace login social con Google/Facebook y no tiene cuenta existente).

O sea: **todo registro de un suscriptor nuevo, sin importar el origen, fallaba siempre.** Los suscriptores que ya existían no se vieron afectados (conservan su código viejo, no se tocan).

## Por qué no se notó antes

El flujo tiene dos puntos de contacto con PanAccess para un registro nuevo:

1. Chequeo de unicidad (`_code_exists_in_panaccess`, vía `getSubscriber`) -- este SÍ fallaba con el mismo error, pero queda atrapado por un `except Exception: return False` genérico (pensado para "si no se puede verificar, asumí que no existe y seguí") -- así que el rechazo de PanAccess se interpretaba silenciosamente como "código disponible", sin ningún log de error visible para un humano.
2. Creación real (`addSubscriber`) -- acá el mismo rechazo SÍ se propaga como `PanAccessException`, capturada en el `except` más externo de `create_subscriber_view`/`_create_subscriber_core`, que responde con un 500 controlado y un mensaje genérico ("No pudimos completar tu registro en este momento...").

Como el 500 es "manejado" (no un crash sin control), no generaba una alerta obvia -- hasta que se activó el sistema de logs de diagnóstico (`applogs`, 2026-09-01) y empezó a agrupar y alertar por email cada vez que el conteo de ocurrencias cruzaba un múltiplo de 50. La primera ocurrencia registrada es del mismo día que se activó ese sistema (2026-09-01 22:22:59) -- probable coincidencia de que el bug ya llevaba tiempo activo y el sistema de logs simplemente empezó a verlo desde que existió.

## El fix

Se sacó el `$` de los 4 prefijos -- son solo texto, nada más depende de ese carácter puntual:

```python
MANUAL_CODE_PREFIX = "BM"
SOCIAL_PROVIDER_CODE_PREFIXES = {"google": "BG", "facebook": "BF"}
DEFAULT_SOCIAL_CODE_PREFIX = "BG"
MANUAL_AUTO_CODE_PREFIX = "BMAUTO"
```

Se revisó el resto del código (`wind/services/subscriber_auth.py`, `wind/utils/email_validation.py`, `wind/adapters.py`) -- todos importan las constantes desde `create_subscriber.py` en vez de hardcodear el prefijo con `$`, así que el fix se propaga solo, sin tocar más archivos.

## Cómo se verificó

- `python3 -m py_compile` limpio sobre `wind/functions/create_subscriber.py`.
- `manage.py check` sin problemas, contra Postgres real (`pgserver`).
- Nuevos tests (`wind/tests/test_subscriber_code_prefix_alphanumeric.py`, 7 tests):
  - Los 4 constantes de prefijo son alfanuméricos puros (regex, directo).
  - El `code` que efectivamente se manda a `addSubscriber` (capturado del mock de PanAccess) es alfanumérico para los 3 orígenes: registro manual con documento, registro manual sin documento (progresivo), y registro social (llamando `_create_subscriber_core` directo, mismo camino que usa el aprovisionamiento de login social).
- Se corrió también la suite existente `wind/tests/test_create_subscriber_hybrid.py` (2 tests) para confirmar que el cambio de prefijo no rompe el modo "hybrid" de aprovisionamiento -- log real de la corrida confirma `'subscriber[code]': 'BM40298765433'` (antes hubiera sido `'BM$40298765433'`).
- **9/9 tests OK.**

## Qué falta (fuera de este fix puntual)

- **Desplegar a producción** -- como todo lo demás de este engagement, esto vive en el repo local hasta que se corra el proceso de deploy real.
- **Los ~100+ usuarios que intentaron registrarse entre 2026-09-01 y 2026-09-07 y fallaron no se recuperan solos** -- no quedó ningún registro parcial en PanAccess (el `addSubscriber` real nunca tuvo éxito), así que no hay nada que limpiar del lado de PanAccess. Del lado local, `SubscriberDocumentRegistry`/`SubscriberEmailRegistry` tampoco se llegaron a escribir para estos intentos fallidos (esas escrituras ocurren después del `addSubscriber` exitoso) -- cualquiera de esas personas puede simplemente reintentar el registro una vez desplegado el fix, sin quedar bloqueado por un registro fantasma.
- **El `except Exception: return False` de `_code_exists_in_panaccess`** (`wind/utils/subscriber_code_generator.py`) sigue igual -- es lo que ocultó este bug durante 6 días sin que nadie lo notara antes de las alertas de `applogs`. No se tocó en este fix (es un comportamiento "fail-safe" razonable en general, no exclusivo de este bug), pero vale la pena una revisión aparte de si conviene diferenciar "PanAccess rechazó por formato inválido" de "no se pudo verificar" en el log, para que un error de este tipo sea visible sin depender únicamente del sistema de logs de diagnóstico.

## Archivos tocados

- `wind/functions/create_subscriber.py` (4 constantes de prefijo)
- `wind/tests/test_subscriber_code_prefix_alphanumeric.py` (nuevo, 7 tests)
