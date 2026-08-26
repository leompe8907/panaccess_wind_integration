# Modo híbrido de aprovisionamiento de suscriptores (Alto #3)

Fecha: 2026-08-26
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Alto #3
Estado: **Implementado, default apagado (modo `sync`, sin cambios de comportamiento).**

## De qué se trata

`create_subscriber_view` (registro público de suscriptores) encadenaba hasta 10 llamadas síncronas a PanAccess dentro de un mismo request HTTP: crear el suscriptor, buscarlo, agregar contactos (email/teléfono), validarlos, bloquear la licencia, releer smartcards y asignar el producto de prueba. Cada llamada puede tardar hasta ~52s (timeout + reintento) si PanAccess está lento, así que en el peor caso un solo registro podía retener un "carril" del servidor 8-9 minutos. Con varios registros concurrentes y PanAccess degradado, esto podía agotar los recursos disponibles para todo el sitio, no solo para el registro.

Ya existía una mitigación (`CREATE_SUBSCRIBER_ASYNC_ENRICHMENT`): solo `addSubscriber` corre sync, el resto se manda siempre a una tarea de Celery (`finish_subscriber_provisioning_task`, ya con reintento y backoff propios). El costo: la respuesta inmediata deja de incluir `token`/`credentials_url`/`license_block_added`/`contacts_added`/`assigned_smartcards`, lo que requiere coordinar un cambio de contrato con las apps antes de activarlo -- por eso seguía apagado.

## Qué se agregó: modo `hybrid`

Un tercer modo que combina lo mejor de los dos: intenta los 10 pasos síncronos, igual que hoy, pero con un presupuesto de tiempo total (`CREATE_SUBSCRIBER_SYNC_BUDGET_SECONDS`, default 8s). Si PanAccess anda normal (el caso de todos los días), la respuesta es **idéntica** a la de hoy, con todos los campos, sin ningún cambio de contrato. Si el presupuesto se agota a mitad de camino, se corta ahí y el resto se manda a la misma tarea de background que ya usa el modo `async` -- que es segura de invocar aunque algunos pasos ya se hayan intentado sync antes de cortar (PanAccess trata los pasos ya hechos como "ya existe"/no-op, confirmado en el docstring de `finish_subscriber_provisioning_task`, `wind/tasks.py:513-516`).

## Qué se implementó

- **`appConfig.py`** (`FeatureConfig`): nuevas variables `CREATE_SUBSCRIBER_PROVISIONING_MODE` (`sync`/`async`/`hybrid`, default `sync`; si no está seteada cae al flag viejo `CREATE_SUBSCRIBER_ASYNC_ENRICHMENT` para no romper despliegues existentes) y `CREATE_SUBSCRIBER_SYNC_BUDGET_SECONDS` (default 8).
- **`wind/functions/create_subscriber.py`**: la rama que antes solo chequeaba el booleano viejo ahora resuelve `provisioning_mode` y, si es `hybrid`, calcula un `sync_deadline` (`time.monotonic() + presupuesto`). Se agregó una función interna `_handoff_to_background()` (reemplaza el bloque de código que antes solo corría en modo async) y 3 checkpoints de presupuesto, antes de cada bloque de trabajo restante: antes de buscar el suscriptor en PanAccess, antes de agregar contactos, y antes del license block/producto de prueba. Si el presupuesto ya se agotó en cualquiera de esos puntos, se corta y se delega el resto.
- **`.env`**: `CREATE_SUBSCRIBER_PROVISIONING_MODE=sync` y `CREATE_SUBSCRIBER_SYNC_BUDGET_SECONDS=8` agregados explícitamente (mismo comportamiento que sin ellas, solo para que quede documentado en el propio archivo).

## Por qué es seguro activarlo (o no) sin drama

- Con `CREATE_SUBSCRIBER_PROVISIONING_MODE=sync` (el default actual), `sync_deadline` queda en `None` y ninguno de los 3 checkpoints se dispara nunca -- el código corre exactamente igual que antes de este cambio.
- Los pasos que ya se intentaron sync antes de un corte no se duplican al reintentarse en background -- ya estaba probado y documentado así para el modo `async` existente, y el modo `hybrid` reutiliza la misma tarea sin modificarla.
- Revertir es un solo valor de configuración (`CREATE_SUBSCRIBER_PROVISIONING_MODE=sync`), sin necesidad de redeploy de código.

## Verificación hecha

- `python3 -m py_compile` sobre ambos archivos modificados -- sin errores.
- `python manage.py check` -- "System check identified no issues".
- `manage.py shell`: confirmado que `FeatureConfig.CREATE_SUBSCRIBER_PROVISIONING_MODE` resuelve a `"sync"` y `CREATE_SUBSCRIBER_SYNC_BUDGET_SECONDS` a `8` con la configuración actual del `.env` (o sea, comportamiento sin cambios por defecto).

## Pendiente antes de activar `hybrid` (o `async`) en producción

1. **Coordinar con iOS/Android** el caso "parcial": cuando se dispara el corte, la respuesta trae `"provisioning_status": "partial"` en vez de `token`/`credentials_url`/etc. Las apps necesitan saber qué hacer en ese caso (por ejemplo, no asumir que el registro está 100% listo para mostrar credenciales de inmediato).
2. **Instrumentar/loguear cuántas veces se dispara el corte en la práctica** una vez activado, para poder ajustar `CREATE_SUBSCRIBER_SYNC_BUDGET_SECONDS` con datos reales en vez de a ciegas (ya queda un log `[Provisioning] Resto del aprovisionamiento de %s encolado en background (modo=%s, motivo=%s)` para esto -- se puede armar una alerta/métrica sobre esa línea).
3. Probar en un entorno de prueba con un presupuesto bajo a propósito (1-2s) simulando una llamada lenta a PanAccess, para confirmar en la práctica que el corte y el traspaso a background funcionan como se espera, antes de activarlo en producción.
