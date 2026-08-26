# Login manual: throttle propio + caché de descubrimiento + bloqueo por intentos fallidos (Alto #5)

Fecha: 2026-08-26
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Alto #5
Estado: **Implementado, verificado localmente (py_compile / manage.py check). Sin cambios de esquema -- no requiere migración ni ventana de mantenimiento.**

## De qué se trata

`POST /api/auth/login/` no tenía límite de tasa propio: caía en el límite genérico anónimo (`AnonBurstThrottle`, 60/minute), pensado para navegación normal del sitio, no para intentos de autenticación. Además, cuando el `login` es numérico y no está en caché local, `verify_panaccess_credentials()` puede terminar llamando a `_discover_login_by_login1()`, que en el peor caso hace **hasta 40 llamadas reales a PanAccess** (`PANACCESS_LOGIN_DISCOVERY_MAX_CALLS`) para encontrar el suscriptor. Con 60/min de margen, un intento de fuerza bruta desde una sola IP podía traducirse en miles de llamadas por minuto al proveedor externo. Tampoco existía ningún bloqueo de cuenta tras varios intentos fallidos seguidos.

Se evaluaron 3 alternativas de alto nivel (throttle dedicado / caché de descubrimiento / bloqueo de cuenta) y se implementaron las 3, integradas.

## Qué se implementó

### Fase 1 -- Throttle dedicado para login manual

- `wind/throttles.py::LoginThrottle(AnonRateThrottle)`, `scope = "login"`.
- `appConfig.py::ThrottleConfig.LOGIN` (env `DRF_THROTTLE_LOGIN`, default `10/minute`).
- `panaccess_wind_integration/settings.py::DEFAULT_THROTTLE_RATES['login']`.
- `panaccess_wind_integration/urls.py`: `path('api/auth/login/', LoginView.as_view(throttle_classes=[LoginThrottle]), ...)`.

Mismo patrón ya usado para `SocialLoginThrottle`/`DeviceSessionThrottle` -- nada nuevo en el mecanismo, solo un scope propio con un límite más ajustado que el genérico anónimo.

### Fase 2 -- Caché de "no encontrado" en el descubrimiento por `login1`

`_discover_login_by_login1()` (`wind/services/subscriber_auth.py`) ahora, antes de arrancar la búsqueda, revisa una clave de caché (Redis, vía `django.core.cache`) `wind:login1_discovery_miss:<login1>`. Si existe, devuelve `None` de inmediato sin hacer ninguna llamada a PanAccess. Si la búsqueda completa termina sin encontrar nada (login1 inexistente, o existente pero con contraseña incorrecta), se guarda esa clave con TTL corto (`PanaccessConfig.LOGIN_DISCOVERY_MISS_CACHE_SECONDS`, env `PANACCESS_LOGIN_DISCOVERY_MISS_CACHE_SECONDS`, default 300s = 5 minutos).

**Por qué es seguro cachear un resultado "no encontrado" incluso cuando la causa fue contraseña incorrecta (no ausencia real del suscriptor):** dentro de `try_codes()`, apenas se identifica el código del suscriptor candidato se llama `fetch_login_info_for_subscriber(subscriber_code=code)`, que persiste `login1` en `SubscriberLoginInfo` localmente -- **sin importar si la contraseña coincidió o no**. Esto significa que, si el suscriptor sí existe, un segundo intento (con la contraseña correcta o incorrecta) ya lo encuentra por la vía rápida local (`find_login_record()`, revisada al principio de `verify_panaccess_credentials()`) y **nunca vuelve a llegar** hasta `_discover_login_by_login1()`. O sea: esta función solo se vuelve a invocar para el mismo `login1` cuando genuinamente no hay ningún suscriptor local con ese número -- ahí sí es correcto (y deseable) recordar el "miss" un rato y no repetir el barrido completo. No se cachea la contraseña en ningún momento, solo el hecho de que la búsqueda no dio resultado.

### Fase 3 -- Bloqueo temporal de cuenta tras intentos fallidos

**Corrección sobre el plan original:** se había considerado reutilizar el mecanismo ya existente en `SubscriberInfo.is_locked()`/`lock_account()`/`failed_login_attempts`/`locked_until` (`wind/models.py`). Al revisar el código antes de implementar, se confirmó que `SubscriberInfo` es el modelo de **perfil de smartcard/activación** (se crea vía un serializer aparte, tiene campos como `sn`, `pin_hash`, `activate()`) y **no** se consulta ni se actualiza en ningún punto del login manual -- el modelo que sí se usa ahí es `SubscriberLoginInfo`, una tabla completamente distinta, sin esos campos de bloqueo. Wirear el bloqueo sobre `SubscriberInfo` habría quedado sin efecto real para la mayoría de las cuentas (cualquiera sin fila en esa tabla, o cuyo `login1`/`login2` ahí no estuviera sincronizado).

En su lugar se implementó sobre caché (Redis, mismo backend que Fase 2), por identificador de login normalizado (el texto tal cual lo tipeó el usuario -- email, código, login1 o login2 -- en minúsculas y sin espacios):

- `wind/services/subscriber_auth.py`: `authenticate_portal_user()` se dividió en un wrapper delgado (mismo nombre, misma firma, mismos callers) que:
  1. Si el identificador está bloqueado (`_is_login_locked`), rechaza de inmediato -- ni siquiera intenta autenticar contra la BD local.
  2. Si no, delega en `_authenticate_portal_user_core()` (el cuerpo que existía antes, sin cambios de lógica salvo la deduplicación de `resolve_subscriber_code` ya descrita en `docs/OPTIMIZACION_LATENCIA_LOGIN_2026-08-26.md`).
  3. Según el resultado: éxito limpia el contador (`_clear_failed_logins`); fallo lo incrementa (`_register_failed_login`), y si llega al umbral, bloquea el identificador por un tiempo.
- `appConfig.py::AuthLockoutConfig`: `ENABLED` (env `LOGIN_LOCKOUT_ENABLED`, default `True`), `MAX_ATTEMPTS` (env `LOGIN_LOCKOUT_MAX_ATTEMPTS`, default 5), `WINDOW_SECONDS` (env `LOGIN_LOCKOUT_WINDOW_SECONDS`, default 300 -- ventana en la que se cuentan los intentos fallidos antes de que el contador expire solo), `LOCKOUT_SECONDS` (env `LOGIN_LOCKOUT_DURATION_SECONDS`, default 900 -- 15 minutos de bloqueo).

## Cómo interactúan las 3 fases en un intento de fuerza bruta

Un atacante probando login1 numéricos al azar contra una sola IP: el throttle (Fase 1) limita a 10 intentos/minuto por IP sin importar nada más. De esos, cada login1 que no existe dispara como máximo una vez el barrido completo de descubrimiento (hasta 40 llamadas a PanAccess); los siguientes intentos contra el mismo número, dentro de los 5 minutos siguientes, no vuelven a tocar PanAccess (Fase 2). Y si el atacante concentra los intentos contra un identificador real (ej. el email de una víctima conocida) probando contraseñas, al quinto intento fallido en la ventana de 5 minutos ese identificador queda bloqueado 15 minutos, sin importar cuántas veces lo reintente (Fase 3) -- y mientras está bloqueado, ni siquiera se gasta el trabajo de `_authenticate_portal_user_core()`.

## Qué queda fuera de este cambio

- El bloqueo es por identificador de *login* tal cual se tipeó, no por subscriber_code resuelto -- si alguien intenta el mismo abonado con dos formas distintas de identificarse (ej. código y luego email), cuenta como dos identificadores separados para el contador. Unificarlo por subscriber_code habría significado resolverlo *antes* de saber si las credenciales son válidas, agregando de vuelta la latencia que se buscaba reducir en el otro documento -- se prefirió esta compensación.
- No hay notificación al usuario cuando su cuenta queda bloqueada (ni email ni respuesta distinta en la API más allá del login rechazado) -- se puede agregar como mejora aparte si se quiere.
- El contador/bloqueo vive en Redis (`django_redis`, mismo backend que el resto de la caché de Django) -- si se necesita desbloquear una cuenta a mano en producción, alcanza con borrar las claves `wind:login_lockout:<identificador>` y `wind:login_failcount:<identificador>` desde `redis-cli` o desde `manage.py shell` (`from django.core.cache import cache; cache.delete(...)`).

## Verificación hecha

- `python3 -m py_compile` sobre `wind/throttles.py`, `wind/services/subscriber_auth.py`, `appConfig.py`, `panaccess_wind_integration/settings.py`, `panaccess_wind_integration/urls.py` -- sin errores.
- `python3 manage.py check` -- "System check identified no issues".
- **Pendiente (requiere producción):** confirmar en vivo que el throttle responde `429` al superar 10 intentos/minuto por IP, que un login1 inexistente repetido dos veces seguidas no dispara el segundo barrido completo (se puede ver en los logs: `_discover_login_by_login1` no debería volver a listar suscriptores), y que al quinto intento fallido con las mismas credenciales el sexto intento (aunque sea con la contraseña correcta) es rechazado por bloqueo -- y que tras `LOGIN_LOCKOUT_DURATION_SECONDS` vuelve a aceptar.

## Cómo desplegar y verificar en producción

1. `git pull` en `/opt/panaccess-wind` -- no hay migración nueva en este cambio (sí la hay en `docs/OPTIMIZACION_LATENCIA_LOGIN_2026-08-26.md`, aplicarla junto si se despliega todo en el mismo pase).
2. Confirmar que las variables nuevas quedaron en `.env`: `DRF_THROTTLE_LOGIN`, `PANACCESS_LOGIN_DISCOVERY_MISS_CACHE_SECONDS`, `LOGIN_LOCKOUT_ENABLED`, `LOGIN_LOCKOUT_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_WINDOW_SECONDS`, `LOGIN_LOCKOUT_DURATION_SECONDS`.
3. Reiniciar los 8 procesos Daphne.
4. Prueba segura sugerida (sin afectar cuentas reales): elegir un `login1` numérico que **no** exista (ej. `999999999`), intentar login dos veces seguidas con cualquier contraseña y revisar los logs (`journalctl -u panaccess-wind@8000.service`) -- el segundo intento no debería mostrar el patrón de múltiples llamadas de `_discover_login_by_login1` (buscar el log de advertencia `"Descubrimiento login1: error listando suscriptores"` o simplemente contar cuántas veces se llama `fetch_login_info_for_subscriber` en cada intento). Para el bloqueo, se puede probar contra la misma cuenta sintética usada en la verificación del Alto #4 (`TEST_ALTO4_BORRAR` o una nueva creada para la prueba), fallando la contraseña 5 veces seguidas y confirmando que el sexto intento -- incluso con la contraseña correcta -- es rechazado.
