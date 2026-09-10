# Dos fixes a partir de la auditoría de logs de producción (2026-09-10)

Fecha: 2026-09-10. Origen: análisis de `logs/errors.log*`/`django.log*`/`tasks.log*` descargados del servidor real (~2 meses, jul-sep 2026).

## 1. `ALLOWED_HOSTS` no incluía el dominio raíz `wind.do`/`www.wind.do`

**Qué se encontró:** el `.env` de producción solo listaba `backend.wind.do, localhost, 127.0.0.1, 192.168.1.183, 190.122.96.188, windtv.wind.do, tv.wind.do` -- nunca `wind.do` ni `www.wind.do`. Resultado: 4,642 rechazos `DisallowedHost` (400) para `wind.do` y 16 para `www.wind.do`, en ráfagas de cientos por día (25, 28, 29 y 31 de agosto concentran la mayoría), y volvió a pasar el mismo 10 de septiembre a las 01:59 -- o sea, seguía pasando el mismo día de este análisis.

**Fix:** agregado `wind.do,www.wind.do` a `ALLOWED_HOSTS` en `.env`:

```
ALLOWED_HOSTS=backend.wind.do,wind.do,www.wind.do,localhost,127.0.0.1,192.168.1.183,190.122.96.188,windtv.wind.do,tv.wind.do
```

**Importante -- esto no es un cambio de código, es un valor de configuración.** El `.env` editado acá es el de este repo de trabajo (`D:\Back-Wind-V2\.env`), no el del servidor real -- **hace falta replicar el mismo cambio en el `.env` de producción y reiniciar el proceso de Django/Gunicorn/Daphne** para que tenga efecto ahí. Sin ese paso manual, el fix no llega a producción.

**Por qué no se investigó más a fondo el origen del tráfico:** los rechazos se concentran en días puntuales con ráfagas de cientos (no tráfico parejo todos los días), lo que sugiere algo específico (una integración, un monitor de uptime, un redirect) pegándole al dominio raíz en esas fechas puntuales -- no se identificó la fuente exacta porque los logs no traen esa información (no había IP asociada a estos rechazos de nivel de middleware, ocurren antes de que `RequestContextMiddleware` exista en el request). Si se repite después de este fix, revisar `nginx`/DNS para confirmar qué realmente resuelve a este backend bajo esos dos hosts.

## 2. `/wind/create-subscriber/` devolvía 500 en vez de 400 cuando PanAccess rechaza el `code` por formato

**Qué se encontró:** 137 ocurrencias de `Internal Server Error: /wind/create-subscriber/` en los logs, con tendencia creciente (6 en agosto → 54 el 6 de septiembre), siempre precedidas por el mismo par de fallos de PanAccess: `Llamada 'getSubscriber' falló` y `Llamada 'addSubscriber' falló`, ambas con el mensaje `El valor de 'code' no es un valor alfanumérico válido. (a-z, A-Z, 0-9)`.

**Causa raíz:** `wind/services/panaccess_client.py::call()` ya lanza `PanAccessAPIError` (no la excepción base) cuando PanAccess responde `success: false` -- pero `wind/functions/create_subscriber.py` (la vista `create_subscriber_view`, vía `_create_subscriber_core`) solo tenía un `except PanAccessException` genérico, sin ningún `except PanAccessAPIError` específico antes -- así que este rechazo de PanAccess (un error de **input del cliente**, no un fallo real de servidor) terminaba devuelto como `HTTP_500_INTERNAL_SERVER_ERROR`. Dos endpoints hermanos (`wind/functions/change_password.py`, `wind/api/profile/views.py::profile_password_view`) ya tenían el patrón correcto: atrapar `PanAccessAPIError` **antes** del genérico y devolver 400 con un `code` estable -- `create_subscriber.py` era el único que no lo seguía.

**Fix:** agregado en `wind/functions/create_subscriber.py`, antes del `except PanAccessException` existente (línea ~1189):

```python
except PanAccessAPIError as e:
    logger.error(f"Error de PanAccess (input inválido): {str(e)}")
    release_registration_locks(registration_locks)
    return Response({
        'success': False,
        'error_type': 'PanAccessAPIError',
        'code': 'subscriber_rejected_by_panaccess',
        'panaccess_error_code': getattr(e, 'error_code', None),
        'message': str(e),
    }, status=status.HTTP_400_BAD_REQUEST)
```

El `except PanAccessException` genérico (para fallos de infraestructura reales -- timeout, conexión, sesión) se deja intacto, sigue devolviendo 500.

**Callers que no se ven afectados:** `wind/services/social_login_provisioning.py::create_subscriber_in_panaccess` llama a `_create_subscriber_core` directo (no vía HTTP) y solo lee `response.data.get("success")` -- sigue siendo `False` en este caso, así que su comportamiento (fallback a `_link_registry_from_list_subscriber`) no cambia; solo mejora el `message` que loguea (ahora el texto real de PanAccess en vez del genérico "No pudimos completar tu registro...").

### Cómo se verificó

`py_compile` sobre los archivos tocados, `manage.py check` sin issues, y una corrida real contra Postgres (harness efímero con `pgserver`):

- Nuevo test `test_invalid_subscriber_code_returns_400_not_500` (`wind/tests/test_auth.py`) -- confirma 400 + `code=subscriber_rejected_by_panaccess` + `error_type=PanAccessAPIError` cuando `addSubscriber` lanza `PanAccessAPIError` con el mensaje real observado en producción.
- Suite completa de creación de suscriptor: `test_auth`, `test_create_subscriber_hybrid`, `test_subscriber_code_prefix_alphanumeric`, `test_social_login`, `test_register_view_feature_flag` -- 32 tests, **OK** (incluye los casos de éxito, duplicados, aprovisionamiento híbrido y login social, para confirmar que el `except` nuevo no interceptó ningún caso que antes pasaba distinto).
