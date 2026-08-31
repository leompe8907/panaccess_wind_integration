# Limpieza de código muerto — Bajo #25

Fecha: 2026-08-28

## Qué

Hallazgo Bajo #25 de `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`: código muerto acumulado (subsistema de rate-limit por WebSocket abandonado, serializers sin uso, imports muertos). Se limpió en tres partes.

### 1. Subsistema de rate-limit por WebSocket abandonado

`wind/utils/websocket_utils.py` tenía dos sistemas de límite de conexiones en paralelo:

- `check_websocket_rate_limit()` / `increment_websocket_connection()` / `decrement_websocket_connection()` — el original, basado en cache de Django.
- `check_websocket_limits()` / `decrement_websocket_limits()` — el que realmente usa `consumers.py` hoy.

El primero ya no lo llamaba nadie (`consumers.py` solo importa el segundo desde hace tiempo); solo quedaba mencionado en comentarios explicativos de `wind/tests/test_websocket.py`. Se eliminaron las 3 funciones y se actualizó el comentario del test para que quede claro por qué ya no se mockean.

### 2. Nueve serializers sin uso

Se identificaron con un script que lista todas las clases `*Serializer` del repo y cuenta referencias cruzadas por grep, y se confirmó manualmente que ninguna se usa como campo anidado dentro de otro serializer del mismo archivo. Una última pasada de grep sobre **todo** el repo (no solo `wind`/`telemetry`) confirmó que las únicas coincidencias eran las propias definiciones y menciones en documentación de auditoría histórica (`REVISION_INDEPENDIENTE_2026-08-14.md`, `AUDITORIA_DECISIONES_Y_PENDIENTES.md`) — cero uso real en código.

Eliminados de `wind/serializers.py`:
- `ListOfSmartcardsSerializer`
- `SubscriberLoginInfoSerializer`
- `SubscriberInfoSerializer`
- `ContactSerializer`
- `AddressSerializer`
- `UDIDAuthRequestSerializer`
- `AuthAuditLogSerializer`

Eliminados de `wind/api/profile/serializers.py`:
- `ProfileMeSerializer`
- `ProfileProductSerializer`

Como consecuencia, en `wind/serializers.py` los imports de modelos `ListOfSmartcards`, `SubscriberLoginInfo` y `AuthAuditLog` quedaron sin uso y también se quitaron. `SubscriberInfo` y `UDIDAuthRequest` **se mantienen** en el import: los sigue usando `UDIDAssociationSerializer.validate()`, que no se tocó. En `wind/api/profile/serializers.py` se quitaron también los imports que solo usaban las dos clases eliminadas (`ListOfProducts`, `ListOfProductsSerializer`).

### 3. Imports muertos

Detectados con `pyflakes` (instalado para esta tarea) y limpiados uno por uno, verificando cada caso antes de borrar (p. ej. `getSubscriber.py` tenía un import de `timezone` a nivel de módulo que ya no se usaba porque había un import local equivalente dentro de la función):

| Archivo | Import(s) eliminado(s) |
|---|---|
| `wind/consumers.py` | `hmac` |
| `wind/models.py` | `uuid` |
| `wind/permissions.py` | `IsAuthenticated` |
| `wind/serializers.py` | `ListOfSmartcards`, `SubscriberLoginInfo`, `AuthAuditLog` (modelos) |
| `wind/views.py` | `IsAuthenticated`, `SubscriberInfo` |
| `wind/functions/getSmartcard.py` | `transaction`, `ListOfSmartcardsSerializer` |
| `wind/functions/getSubscriber.py` | `from django.utils import timezone` (top-level, había un re-import local) |
| `wind/management/commands/send_bulk_test_emails.py` | `CommandError` |
| `wind/tests/test_websocket.py` | `json`, `settings`, `MagicMock` |
| `wind/utils/websocket_utils.py` | `os`, `timedelta` |
| `telemetry/services/panaccess_ott_ingest.py` | `Tuple` (de `typing`) |

`telemetry/tests.py` se revisó y se dejó igual: el `from django.test import TestCase  # noqa: F401` es un placeholder intencional de Django, no código muerto real.

## Por qué

Código muerto (funciones sin llamador, serializers sin ninguna vista/servicio que los use, imports que no aportan nada) no es un riesgo de seguridad directo, pero sí un costo de auditoría real: cada vez que se revisa el código hay que evaluar si esas piezas están en uso antes de poder descartarlas con confianza (como pasó varias veces en esta misma auditoría con hallazgos que resultaron ya resueltos). Menos superficie muerta implica revisiones más rápidas y menos falsos candidatos a "esto quizás haga algo".

## Cómo se verificó

1. `pyflakes` sobre cada archivo tocado — 0 warnings tras la limpieza.
2. Antes de borrar los 9 serializers: grep de sus nombres sobre el repo completo (excluyendo `.git`/`env`/`node_modules`), confirmando 0 referencias en código, solo en dos documentos de auditoría histórica.
3. `python3 -m py_compile` sobre `wind/serializers.py` y `wind/api/profile/serializers.py` — OK.
4. `python3 manage.py check` — `System check identified no issues (0 silenced)`.

## Archivos tocados

- `wind/serializers.py`
- `wind/api/profile/serializers.py`
- `wind/utils/websocket_utils.py`
- `wind/consumers.py`
- `wind/models.py`
- `wind/permissions.py`
- `wind/views.py`
- `wind/functions/getSmartcard.py`
- `wind/functions/getSubscriber.py`
- `wind/management/commands/send_bulk_test_emails.py`
- `wind/tests/test_websocket.py`
- `telemetry/services/panaccess_ott_ingest.py`
- `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (fila Bajo #25 actualizada)
