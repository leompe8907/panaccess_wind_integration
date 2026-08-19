# Revisión técnica independiente — Back-Wind-V2

**Fecha:** 2026-08-14
**Metodología:** revisión hecha desde cero, leyendo directamente el código fuente actual (`wind/`, `panaccess_wind_integration/`, `appConfig.py`, `deploy/`). **No se consultó ni se asumió como válida la documentación previa en `docs/`** (auditorías anteriores), tal como se solicitó — cualquier coincidencia con hallazgos previos es porque el problema sigue existiendo en el código, no porque se haya copiado de ahí.

Alcance cubierto: mapa de funcionalidades y flujos (huérfanos/incompletos), modelo de datos y configuración de BD, integraciones externas y jobs en background, seguridad de autenticación/permisos, rendimiento y resiliencia operacional, y código muerto/cobertura de tests.

---

## Resumen ejecutivo

El proyecto está, en general, **notablemente bien construido y ya auditado activamente por el equipo**: cero vistas huérfanas, cero flujos rotos entre templates y URLs, manejo consistente de errores de conexión a BD en hilos/Celery, un circuit breaker real (no decorativo) para la integración con PanAccess, y buenas prácticas de idempotencia en las sincronizaciones masivas. Los comentarios en el código documentan explícitamente varios incidentes de producción ya resueltos (fugas de conexión, condiciones de carrera, excepciones de WebSocket mal codificadas).

Sin embargo, la revisión encontró **un hallazgo crítico de seguridad que anula buena parte de ese trabajo de endurecimiento**: las rutas nativas de `dj_rest_auth` para reset de contraseña y registro siguen montadas y activas, y evaden por completo la lógica propia de sincronización con PanAccess, invalidación de sesión y límites de tasa. Además hay un problema de rendimiento serio (registro público puede bloquear un worker completo varios minutos) y varias inconsistencias de integridad de datos (borrado incompleto de suscriptores, una tabla con lectura activa pero sin escritura conocida).

### Los 5 hallazgos más urgentes

| # | Hallazgo | Severidad | Dominio |
|---|---|---|---|
| 1 | `dj_rest_auth.urls`/`registration.urls` expuestos en `/api/auth/` evaden reset/registro propios (sin sync con PanAccess, sin invalidar sesiones, sin CAPTCHA, throttle real 12x más laxo) | **Crítico** | Seguridad |
| 2 | Secretos reales (`SECRET_KEY`, `ENCRYPTION_KEY`, contraseñas de BD/Redis) presentes en el historial de git | **Crítico** | Seguridad |
| 3 | `create-subscriber` (registro público) encadena hasta 10 llamadas síncronas a PanAccess en un solo request — puede bloquear un proceso Daphne varios minutos | **Alto** | Rendimiento |
| 4 | Login (`/api/auth/login/`) y cambio de contraseña de dj_rest_auth sin throttle real, sin `old_password` y sin invalidar sesiones tras el cambio | **Alto** | Seguridad |
| 5 | Sin FK real entre `ListOfSubscriber` y tablas dependientes (`UDIDAuthRequest`, `DeviceSession`) → dispositivos huérfanos siguen autenticando tras borrar un suscriptor en el sync | **Alto** | Datos/Integridad |

---

## 1. Seguridad

### 1.1 CRÍTICO — Endpoints de `dj_rest_auth` evaden todo el endurecimiento propio (account takeover)

`panaccess_wind_integration/urls.py:17-18` monta:
```python
path('api/auth/', include('dj_rest_auth.urls')),
path('api/auth/registration/', include('dj_rest_auth.registration.urls')),
```

El flujo propio de reset (`wind/api/password_reset/urls.py`, montado en `api/auth/password/`) solo define `forgot/` y `reset-confirm/`. Una petición a `POST /api/auth/password/reset/` **no matchea ese include** y cae en `dj_rest_auth.urls`, que sí registra `password/reset/` y `password/reset/confirm/` de forma nativa.

Esas vistas nativas (`PasswordResetView`/`PasswordResetConfirmView` de dj_rest_auth):
- Generan el token con `django.contrib.auth.tokens.default_token_generator`, no con el `TimestampSigner` propio de `wind/services/password_reset.py`.
- Al confirmar, llaman `SetPasswordForm.save()` directo — **la contraseña en PanAccess nunca cambia**, queda desincronizada de la de Django.
- No invocan `mark_password_changed` → los JWT/refresh tokens emitidos antes del reset **siguen siendo válidos**.
- No invocan `revoke_all_device_sessions_for_subscriber` → los dispositivos/TVs vinculados no se desconectan.
- No pasan por `wind/utils/recaptcha.py` → sin CAPTCHA.
- No pasan por `wind/utils/password_policy.py` → sin la política de contraseñas propia del negocio.
- Declaran `throttle_scope = 'dj_rest_auth'`, pero `ScopedRateThrottle` **nunca está registrado** en `DEFAULT_THROTTLE_CLASSES` (`settings.py:172-186`) — ese scope es inerte. El único límite real aplicado es el genérico `AnonBurstThrottle` (60/min), **12x más permisivo** que el `PasswordResetThrottle` (5/hora) diseñado para este flujo.

Lo mismo aplica a `POST /api/auth/registration/` (`RegisterView` nativo): crea usuarios Django directamente, **sin pasar por** `create_subscriber_view` (que aplica `RegisterThrottle`, reCAPTCHA y el flag `CREATE_SUBSCRIBER_PUBLIC_ENABLED`). Si un operador desactiva expresamente el registro público (`CREATE_SUBSCRIBER_PUBLIC_ENABLED=false`) para forzar solo login social, **este endpoint sigue permitiendo crear cuentas igual**.

**Escenario de explotación:** un atacante que conoce el email de una víctima llama `POST /api/auth/password/reset/` (sin CAPTCHA, sin el límite de 5/hora real), luego `POST /api/auth/password/reset/confirm/` con el token recibido. La contraseña local de Django cambia; PanAccess queda con la contraseña original; ningún JWT/sesión de dispositivo existente se invalida. El atacante hace login normal (`/api/auth/login/`), que autentica contra el hash local recién sobrescrito vía `ModelBackend` — **toma control total de la cuenta del portal sin haber tocado PanAccess**.

**Recomendación:** eliminar del `urlpatterns` las subrutas de `dj_rest_auth.urls`/`registration.urls` que no se quieren exponer (dejar solo `login/`, `logout/`, `token/refresh`, `token/verify` si son necesarias), o sobreescribirlas para que devuelvan 404 antes de que dj_rest_auth las registre. Confirmar con un test de integración que `POST /api/auth/password/reset/` y `POST /api/auth/registration/` responden 404.

### 1.2 CRÍTICO — Secretos reales en el historial de git

`git log --follow -- .env` muestra ~19 commits versionando `.env` en texto plano hasta el commit que dejó de trackearlo. `git show <commit-antiguo>:.env` sigue devolviendo `SECRET_KEY`, `ENCRYPTION_KEY`, `DB_PASSWORD`, `REDIS_PASSWORD` y credenciales de PanAccess reales.

**Impacto:** cualquiera con acceso al repo (clones existentes, forks, CI) puede recuperar esos valores del historial aunque `.env` ya no esté trackeado hoy. Con el `SECRET_KEY` histórico se pueden falsificar tokens de reset de contraseña (firmados con `TimestampSigner`, que usa el mismo `SECRET_KEY`); con `ENCRYPTION_KEY` se descifra cualquier `password_hash` guardado con Fernet.

**Recomendación:** rotar de inmediato **todas** las credenciales que aparecieron alguna vez en `.env`, y evaluar reescribir el historial (`git filter-repo`/BFG) si el repositorio tiene o tuvo colaboradores/forks externos.

### 1.3 ALTO — Login sin protección anti fuerza bruta real

`LoginView` (dj_rest_auth) declara el mismo `throttle_scope='dj_rest_auth'` inerte del punto 1.1 — el único límite real es `AnonBurstThrottle` (60/min por IP), sin reCAPTCHA ni bloqueo de cuenta. Además, cuando el login local falla, `authenticate_portal_user` (`wind/services/subscriber_auth.py:323-380`) dispara **llamadas en vivo a PanAccess** para intentar descubrir el registro (hasta 40 llamadas adicionales por intento, `PanaccessConfig.LOGIN_DISCOVERY_MAX_CALLS`), lo que amplifica la carga contra el propio PanAccess durante un ataque de fuerza bruta.

**Recomendación:** un throttle propio de login (bajo, por IP y por identificador de usuario) + reCAPTCHA progresivo tras N fallos.

### 1.4 ALTO — Cambio de contraseña sin verificar la actual y sin cerrar otras sesiones

`/api/auth/password/change/` (dj_rest_auth) no tiene sobreescritos `OLD_PASSWORD_FIELD_ENABLED`/`LOGOUT_ON_PASSWORD_CHANGE` en `REST_AUTH` (`settings.py:195-210`), así que toman el default `False`/`False`: cualquier request con un access token válido (aunque esté robado) puede cambiar la contraseña **sin aportar la actual**, y ni el resto de JWTs ni las `DeviceSession` se revocan después — a diferencia del flujo propio (`profile_password_view`), que sí hace `mark_password_changed` + `revoke_all_device_sessions_for_subscriber`. Tampoco sincroniza con PanAccess.

**Recomendación:** igual que 1.1 — retirar o exigir `old_password` + forzar invalidación de sesiones también en este endpoint nativo, o eliminarlo del todo y dejar solo el propio de `/api/v1/profile/password/`.

### 1.5 ALTO — reCAPTCHA es "opt-in" (fail-open si falta la env var)

`wind/utils/recaptcha.py:21-38`: si `RECAPTCHA_SECRET_KEY` no está en el entorno, **todos** los flujos (registro, forgot-password, confirm-password) quedan sin verificación humana, apoyados solo en throttles por IP fácilmente evadibles con rotación de IP. Es una decisión de diseño válida siempre que la variable esté realmente presente en producción — vale la pena confirmarlo activamente, ya que el fallo es silencioso.

### 1.6 MEDIO — Abuso de prueba gratis vía login social

`SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER=False` por defecto permite que cualquier login social nuevo cree un suscriptor con prueba gratis automáticamente. `GoogleLoginView`/`FacebookLoginView` solo tienen `SocialLoginThrottle` (20/min por IP) y **ningún reCAPTCHA** — con cuentas Google/Facebook desechables scriptable, esto habilita abuso de la prueba gratuita a escala.

### 1.7 MEDIO — AES-CBC sin autenticación en el esquema legado de credenciales para TV

`wind/utils/crypto_tv.py` mantiene `hybrid_encrypt_for_app()` con CBC sin HMAC/GCM (documentado como necesario para no romper un cliente externo ya integrado). El camino nuevo (`hybrid_encrypt_for_device_public_key`) sí usa AES-256-GCM autenticado. Riesgo aceptado y documentado, pero sigue siendo manipulable en tránsito (bit-flipping) para ese camino específico.

### 1.8 MEDIO — WebSockets sin `AllowedHostsOriginValidator`

`panaccess_wind_integration/asgi.py` no envuelve el router de WebSocket con el validador de origen de Channels. Hoy el impacto está acotado porque ambos consumers exigen token explícito por query string (no cookies), pero si en algún momento se activa `JWT_USE_COOKIES=true` se abriría un vector de Cross-Site WebSocket Hijacking. Agregar el validador de todos modos, por defensa en profundidad.

### 1.9 BAJO
- Logout de JWT solo blacklistea el refresh entregado; el access token sigue vivo hasta expirar (mitigado por TTL corto, ~15 min).
- Sin `Content-Security-Policy`/`Permissions-Policy` en las vistas HTML (`login.html`, `register.html`, etc.).
- Password de servicio hacia PanAccess usa MD5+salt (requisito del contrato externo, no controlable desde este código).

### Lo que ya está bien resuelto (para contraste)
`jwt_invalidation.py` sí invalida de verdad (blacklist + rechazo por `iat`); el reset propio usa `TimestampSigner` con expiración y control de reuso en BD; `pre_social_login` exige email verificado por el proveedor antes de fusionar cuentas (y por eso Facebook queda bloqueado hoy — ver 1.10 en Flujos); CORS rechaza `CORS_ALLOW_ALL_ORIGINS` al arrancar; `SECRET_KEY`/`ALLOWED_HOSTS`/`ENCRYPTION_KEY` son obligatorios sin fallback hardcodeado; el middleware de IP para `/wind/sync-*` valida `X-Forwarded-For` contra proxies de confianza.

---

## 2. Funcionalidades, rutas y flujos

### 2.1 Mapa de rutas — sin huérfanas, sin flujos rotos
Se mapearon todas las rutas de `panaccess_wind_integration/urls.py`, `wind/urls.py`, `wind/api/urls.py`, `wind/api/profile/urls.py`, `wind/api/password_reset/urls.py` y el routing de WebSockets (`wind/routing.py`). **Cada vista definida está enlazada en algún urls.py; ningún `{% url %}` de un template apunta a un nombre inexistente.** El flujo registro→email de bienvenida→credenciales y forgot→email→reset están correctamente conectados de punta a punta.

### 2.2 Código muerto de cara a la API (limpieza recomendada, no bugs)
- **8 serializers DRF sin ningún consumidor**: `ContactSerializer`, `AddressSerializer`, `SubscriberLoginInfoSerializer`, `SubscriberInfoSerializer` (con `create()`/`update()` completos incluyendo manejo de password/pin), `UDIDAuthRequestSerializer`, `AuthAuditLogSerializer` (`wind/serializers.py`), `ProfileMeSerializer`, `ProfileProductSerializer` (`wind/api/profile/serializers.py`). Todos reemplazados por funciones "payload builder" ad-hoc en `subscriber_catalog.py` que nunca los retiraron.
- **`change_password_view`** (`POST /wind/change-password/`, `wind/functions/change_password.py:36`) es un duplicado legacy de `profile_password_view` (`/api/v1/profile/password/`) — mismo docstring lo admite ("Preferir: POST /api/v1/profile/password/"). Ningún template interno lo usa.
- **Tres endpoints de diagnóstico solapados** para verificar la sesión de PanAccess: `/wind/ops/panaccess-session/`, `/wind/singleton/`, `/wind/logged-in/` — todos protegidos por `IsAdminUser`, pero funcionalmente redundantes entre sí.
- **Páginas/endpoints "test" expuestos sin gate de entorno**: `subscriber-test/`, `login-test/`, `login-test-facebook/` (públicos, sin chequeo de `DEBUG`/staff) y `test_call_list_products`/`test_call_list_smartcards` (protegidos por `IsAdminUser`, pero siguen siendo llamadas crudas de prueba a PanAccess montadas junto a las rutas reales).

### 2.3 Facebook login: bloqueado permanentemente por diseño (no es un bug, pero es una trampa a futuro)
`allauth`'s Facebook provider siempre devuelve `verified=False` para el email (limitación documentada de la propia librería). Como `pre_social_login` exige email verificado por el proveedor, **todo login con Facebook se rechaza siempre**. Es el comportamiento correcto (fail-closed) dado el objetivo anti-account-takeover, pero es una señal de alerta: si alguien "arregla" el login de Facebook quitando esa verificación para que vuelva a funcionar, reabre el vector de account-takeover que ese chequeo existe para prevenir.

---

## 3. Modelo de datos y base de datos

### 3.1 ALTO — Sin FK real entre `ListOfSubscriber` y sus tablas dependientes → dispositivos huérfanos
Todas las relaciones "de negocio" (`ListOfSubscriber` ↔ `SubscriberLoginInfo`/`SubscriberInfo`/`UDIDAuthRequest`/`DeviceSession`/`SubscriberEmailRegistry`/`AuthAuditLog`) son `CharField` sueltos copiados (`code`/`subscriberCode`), no `ForeignKey`. Se confirmó un camino real que deja huérfanos: el borrado de suscriptores por sync periódico (`getSubscriber.py:240-284` → `delete_subscriber_operational_data(preserve_registry=False)`) limpia `SubscriberLoginInfo`/`SubscriberEmailRegistry`/`SubscriberDocumentRegistry`/`SubscriberInfo`, pero **no toca `UDIDAuthRequest`, `DeviceSession`, `EncryptedCredentialsLog` ni `AuthAuditLog`**. Un dispositivo pareado puede seguir autenticando indefinidamente para un `subscriber_code` que ya no existe. (Sí existe limpieza completa en el flujo explícito de *cierre de cuenta*, pero no en el de sync). Ese mismo borrado tampoco está envuelto en `transaction.atomic()` — un fallo a mitad de camino puede dejar registros antifraude borrados con la fila de suscriptor todavía viva.

### 3.2 ALTO — `SubscriberInfo` se lee en un flujo crítico pero no tiene escritor conocido en este repo
`subscriber_code` no es único (`wind/models.py:251`) y el único `.create()`/`.update_or_create()` de este modelo está en `SubscriberInfoSerializer` (código muerto, ver 2.2). Sin embargo, **sí se lee activamente**: `udid_auth_service.py:127-145` hace `SubscriberInfo.objects.get(...)` para entregar credenciales a Smart TV, devolviendo `"subscriber_not_found"` si no hay filas. Esto sugiere que ese flujo de entrega de credenciales está roto salvo que la tabla se pueble desde un proceso externo a este repositorio — vale la pena confirmarlo con el equipo.

### 3.3 MEDIO — Índices funcionales faltantes en rutas de login/reset calientes
La migración `0004` agregó un índice funcional `Upper(emails)` solo para `ListOfSubscriber.emails`, pero **no** para `SubscriberEmailRegistry.email` (usado con `__iexact` en `password_reset.py`, `social_login_provisioning.py`, `subscriber_auth.py`, `subscriber_catalog.py`, `subscriber_trial.py`) ni para `SubscriberLoginInfo.login2`/`ListOfSubscriber.code` (usados con `__iexact` en el login, `subscriber_auth.py:47,79`). En PostgreSQL, `__iexact` compila a `UPPER(x) = UPPER(...)`, que no puede usar un índice btree plano — exactamente el problema que 0004 corrigió en un solo campo, sin extenderlo a los demás campos consultados igual de intensivamente.

### 3.4 MEDIO — Router de réplica sin failover
`wind/db_router.py` envía todas las lecturas a `'replica'` sin ningún fallback a la primaria si la réplica está caída — si `DB_REPLICA_HOST` está configurado y esa réplica se cae, **toda lectura de la aplicación falla** aunque la primaria esté sana.

### 3.5 MEDIO — Sin timeout de conexión ni manejo de `OperationalError`
`DatabaseConfig` no define `OPTIONS={'connect_timeout': ...}` — si Postgres deja de responder a nivel de red, cada conexión nueva bloquea hasta el timeout TCP del SO. Los `autoretry_for` de Celery cubren errores de PanAccess pero no `django.db.utils.OperationalError`/`InterfaceError` — un corte breve de Postgres hace fallar la tarea definitivamente en vez de reintentar. Tampoco hay manejo HTTP de `OperationalError` (se propaga como 500 genérico).

### 3.6 BAJO
- `AppCredentials.app_type` cambió de vocabulario de `choices` en la migración `0007` sin `RunPython` de backfill — filas antiguas sembradas con el vocabulario viejo dejan de matchear silenciosamente.
- `AuthAuditLog.ACTION_TYPES` incluye `login_attempt`/`login_success`/`login_failed`/`account_locked`/`account_unlocked`, pero ninguno se usa realmente — los intentos y bloqueos de login no quedan auditados pese a que el modelo lo contempla, relevante para forense de fuerza bruta.
- Migraciones `0004`–`0006` traen advertencias propias de "nunca verificadas contra Postgres real", solo contra SQLite en desarrollo.
- **Puntos positivos confirmados**: `CONN_MAX_AGE=60` + `CONN_HEALTH_CHECKS=True` con `close_old_connections()` explícito en hooks de Celery y en hilos de `ThreadPoolExecutor` — corrige un incidente real de agotamiento de pool ya documentado en el propio código. `select_for_update()`+`transaction.atomic()` bien aplicados donde importa (pairing de dispositivos).

---

## 4. Integraciones externas y jobs en background

### 4.1 Integración con PanAccess — robusta, con una asimetría
- Circuit breaker real (no decorativo), con estado en **Redis** (compartido entre los 8 workers), umbral de 5 fallos, recuperación en 60s, excluyendo a propósito errores de negocio para no abrir el circuito por ellos. Sí está conectado a las llamadas reales (`panaccess_singleton.py:213-233`).
- Cliente principal (`panaccess_client.py`) tiene timeout configurable (25s) y reintentos con backoff. Pero `wind/utils/panaccess_auth.py` (usado por login/logged_in) tiene su **propio timeout hardcodeado de 30s y sin reintentos**, inconsistente con el resto de llamadas.
- Sesión de PanAccess compartida vía Redis con lock distribuido — evita "login storms" entre los 8 procesos.

### 4.2 Emails — se degradan con gracia
Ningún fallo de envío de email (bienvenida, cambio de contraseña, reset) revierte ni bloquea la operación principal — todos están envueltos en `try/except`, y el envío real ocurre en tareas Celery con reintento propio. Correctamente diseñado.

### 4.3 Sincronizaciones — buena idempotencia, con protección explícita contra interrupciones
`compare_and_update_all_subscribers`/`_reconcile_subscriber_smartcards` **omiten el borrado local si la paginación remota no se completó**, evitando eliminar abonados que en realidad siguen existiendo en PanAccess tras una corrida cortada a mitad de camino. Todas las escrituras masivas usan `bulk_create(ignore_conflicts=True)`/`bulk_update` por chunk dentro de `transaction.atomic()`. `full_sync` no tiene checkpoint entre sus sub-pasos (subscribers→products→smartcards→login info) — si se interrumpe a mitad, se reinicia desde el principio en el siguiente intento, pero cada sub-paso es idempotente por sí solo.

### 4.4 GeoIP vs reCAPTCHA — contraste correcto de fail-open/fail-closed
GeoIP falla abierto (nunca bloquea, es puramente informativo, documentado explícitamente como "no usar para decisiones de seguridad"). reCAPTCHA, una vez configurado, falla **cerrado** (rechaza ante timeout/error de Google) — diseño consistente con el nivel de criticidad de cada uno.

### 4.5 WebSockets — limpieza consistente, una asimetría menor
Ambos consumers limpian conexiones, grupos y tareas de asyncio al desconectar, y cierran conexiones de BD explícitamente (Channels no dispara las señales que normalmente lo harían). `AuthWaitWS` tiene un `_inactivity_check` de 180s que `DeviceSessionWS` no tiene — una conexión "device" que deja de responder sin cerrar el socket TCP podría quedar viva más tiempo que una de "auth".

---

## 5. Rendimiento y resiliencia operacional

### 5.1 ALTO — Registro público puede bloquear un proceso Daphne completo varios minutos
`create_subscriber_view`, en su modo síncrono por defecto (`CREATE_SUBSCRIBER_ASYNC_ENRICHMENT=False`), encadena **hasta 10 llamadas síncronas a PanAccess dentro de un mismo request HTTP** (crear suscriptor, buscarlo, agregar contacto, validar, agregar producto trial, resolver credenciales para el email...). Con el peor caso documentado de ~54s por llamada, el peor caso teórico de un solo request es de **varios minutos**. Dado el modelo de despliegue (8 procesos Daphne, uno por puerto detrás de nginx), un registro colgado inmoviliza 1/8 de la capacidad total del sitio. Además, nginx no define un `proxy_read_timeout` propio para esta ruta (hereda el default de 60s, menor a los 120s de `location /`), por lo que el cliente puede recibir un 504 mientras el backend sigue procesando minutos más.

La mitigación (`CREATE_SUBSCRIBER_ASYNC_ENRICHMENT=True`) ya existe en el código pero está apagada por defecto porque cambia el contrato de respuesta.

### 5.2 MEDIO — systemd sin protección contra caídas prolongadas de dependencias
Todos los `.service` usan `Restart=always`/`RestartSec=3-5` pero no definen `StartLimitIntervalSec`/`StartLimitBurst`, `LimitNOFILE`, `MemoryMax` ni `OOMPolicy`. Con el default global de systemd (5 reintentos en 10s), una caída prolongada de Postgres/Redis puede agotar los reintentos y dejar el servicio en estado `failed` **permanentemente**, sin volver a intentar aunque la dependencia se recupere — requeriría intervención manual. Tampoco hay techo de descriptores de archivo para las conexiones WebSocket de 24h de duración.

### 5.3 MEDIO — nginx sin `proxy_next_upstream` y con `fail_timeout=0`
El upstream de 8 backends usa `fail_timeout=0`, que desactiva el mecanismo de nginx de recordar temporalmente un backend caído — durante un restart secuencial de las 8 instancias, las requests que caigan justo en la instancia reiniciándose pueden devolver 502 en vez de reintentarse automáticamente en otro de los 7 backends sanos.

### 5.4 Healthcheck real (no trivial) — con un gap menor
`/health/` y `/ready/` hacen verificaciones reales (query a Postgres, roundtrip de Redis, login real a PanAccess protegido por token HMAC para no abusar del login de PanAccess desde un endpoint público). Buen diseño. Gap: no verifica la salud de la réplica de BD ni del broker de Celery — un fallo ahí no se reflejaría en el healthcheck.

### 5.5 BAJO
- `log_buffer.py` lanza un hilo nuevo por cada flush sin esperar al anterior — bajo incidentes sostenidos de BD lenta, pueden convivir varios hilos con conexiones abiertas en paralelo (durabilidad ya cubierta por cola en Redis + tarea de recuperación, así que no hay pérdida de datos).
- Sin caché para `resolve_subscriber_code_for_user`, que repite hasta 3 queries indexadas en cada request autenticado de perfil/productos/dispositivos — barato hoy, candidato a caché si el tráfico escala.
- Tráfico redundante: cada uno de los ~11-12 procesos (8 Daphne + workers Celery) corre su propio hilo de validación periódica de sesión PanAccess cada 15 min, de forma no coordinada (mitigado porque la sesión en sí es compartida vía Redis).
- Contradicción potencial no verificada: si `CELERY_TASK_ALWAYS_EAGER=True` se activara fuera de tests, la sesión de PanAccess dejaría de compartirse vía Redis silenciosamente.

---

## 6. Código huérfano y cobertura de tests

### 6.1 Código muerto confirmado (candidato a limpieza)
- **Subsistema completo de rate-limit de WebSocket abandonado** en `wind/utils/websocket_utils.py` (`check_temp_token_rate_limit`, `check_websocket_rate_limit`, `increment_websocket_connection`, `decrement_websocket_connection`, `check_circuit_breaker` y `track_system_request` como stubs) — el propio código documenta que se consolidó en un sistema nuevo, pero el viejo nunca se borró.
- `wind/functions/__init__.py`: barrel que re-exporta ~19 nombres, de los cuales la mayoría nunca se consume vía el paquete (todo el código real importa directamente de los submódulos) — refactor a medias.
- Funciones sueltas sin caller: `generate_rsa_key_pair`/`verify_app_can_decrypt` (`crypto_tv.py`), `execute_with_reconnect` (`db_utils.py`), `validate_email_and_document` (`email_validation.py`), `flush_logs`/`shutdown_log_buffer` (`log_buffer.py`), `store_login_info_in_chunks` (`getSubscriberLoginInfo.py`).
- `LocustConfig` en `appConfig.py` apunta a un `scripts/load/locustfile.py` que no existe en el repo.
- 8 imports muertos puntuales (`hmac` en `consumers.py`, `uuid` en `models.py`, `IsAuthenticated` en `permissions.py` y `views.py`, etc. — ver detalle del agente).

### 6.2 Huecos de cobertura de tests (54 tests existentes, bien organizados, pero con vacíos en servicios críticos)
Sin ningún test: `panaccess_client.py`, `panaccess_singleton.py`, `panaccess_circuit_breaker.py`, `panaccess_session_store.py` (todo el corazón de la integración externa), el flujo completo de **login social** (`social_login_provisioning.py`, `adapters.py`, `GoogleLoginView`/`FacebookLoginView`), `geo_lookup.py`, el roundtrip real de `encryption.py`, `udid_auth_service.py`, `device_session_service.py`, `jwt_invalidation.py`, `subscriber_provisioning.py`, `registration_lock.py`, `views_health.py` y `device_views.py`.

Dado que el hallazgo crítico de seguridad (1.1) y el de rendimiento (5.1) están precisamente en las áreas de login/reset/registro, y que el circuit breaker y la sesión compartida (que si fallan, tumban la integración con PanAccess para todo el sitio) tampoco tienen test, **estos son los huecos de cobertura con mayor relación riesgo/esfuerzo para cerrar primero**.

---

## Recomendaciones priorizadas

1. **Inmediato** — Bloquear o eliminar las rutas de `dj_rest_auth.urls`/`registration.urls` no usadas (1.1), y rotar todos los secretos que aparecieron alguna vez en `.env` en el historial de git (1.2).
2. **Corto plazo** — Añadir throttle real de login + reCAPTCHA progresivo (1.3); exigir `old_password` e invalidar sesiones en el cambio de contraseña nativo (1.4); confirmar que `RECAPTCHA_SECRET_KEY` está seteada en producción (1.5).
3. **Corto plazo** — Activar o rediseñar `CREATE_SUBSCRIBER_ASYNC_ENRICHMENT` para sacar la cadena de llamadas a PanAccess del request síncrono de registro (5.1); dar timeout propio a esa ruta en nginx.
4. **Medio plazo** — Decidir e implementar una limpieza real de `UDIDAuthRequest`/`DeviceSession`/`AuthAuditLog` cuando un suscriptor se borra por sync (3.1); confirmar si `SubscriberInfo` tiene un poblador externo o si el flujo de credenciales para TV que depende de ella está efectivamente roto (3.2); extender el índice funcional `Upper()` a `SubscriberEmailRegistry.email`, `SubscriberLoginInfo.login2` y `ListOfSubscriber.code` (3.3).
5. **Medio plazo** — Añadir `StartLimitIntervalSec`/`LimitNOFILE`/`MemoryMax` a los `.service` de systemd (5.2); `proxy_next_upstream` + `fail_timeout` distinto de 0 en nginx (5.3); fallback de réplica a primaria en `db_router.py` (3.4).
6. **Cuando haya ventana** — Retirar el código muerto identificado (6.1) y priorizar tests para `panaccess_client`/`panaccess_circuit_breaker`/login social/`jwt_invalidation` antes que para el resto (6.2).

---

*Documento generado por revisión automatizada independiente. Cada hallazgo cita archivo:línea verificable en el código fuente al momento de esta revisión (2026-08-14); confirmar antes de actuar que el código no haya cambiado desde entonces.*
