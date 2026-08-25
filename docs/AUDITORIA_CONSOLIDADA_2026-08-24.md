# Auditoría consolidada — pendientes, errores e incompletitud

Fecha: 2026-08-24

## Metodología

Este documento consolida tres pasadas independientes:

1. **Reconciliación** de los hallazgos abiertos en `AUDITORIA_DECISIONES_Y_PENDIENTES.md`, `REVISION_INDEPENDIENTE_2026-08-14.md`, `RECAPTCHA_ESTADO_Y_PENDIENTES.md` y `GUIA_INTEGRACION_UNIFICADA.md` contra el código real actual (confirmando con `Read`/`Grep`/`git log` si cada hallazgo sigue igual, mejoró o empeoró).
2. **Revisión del código nuevo** agregado en las últimas sesiones: la app `telemetry` completa, el redirector `go_windtv_view`, los cambios de `appConfig.py` y el `.env`.
3. **Barrido fresco** sobre áreas del proyecto que las auditorías previas no habían cubierto todavía (vistas, serializers, middleware, templates, settings, dependencias).

Se excluye deliberadamente todo lo que los documentos ya marcan como "Resuelto" o "decisión de diseño aceptada" salvo que la reconciliación haya encontrado que la situación cambió.

---

## Urgente

| # | Hallazgo | Archivo | Nota |
|---|----------|---------|------|
| 1 | ~~Las rutas nativas `dj_rest_auth.urls` / `dj_rest_auth.registration.urls` / `allauth.urls` evaden reCAPTCHA, la sincronización con PanAccess y el throttle real~~ **RESUELTO (2026-08-25)** -- se recortaron a mano los tres includes, dejando solo login/logout/user/token (dj-rest-auth), los callbacks OAuth de Google/Facebook (allauth), y quitando el registro nativo por completo. Ver `docs/LIMPIEZA_RUTAS_AUTH_NATIVAS_2026-08-25.md`. | `panaccess_wind_integration/urls.py` | Crítico, independiente -- resuelto |
| 2 | Secretos reales versionados en el historial de git: `SECRET_KEY`, `ENCRYPTION_KEY`, `DB_PASSWORD` en claro. Confirmado con `git log --all --full-history -- .env` — 19 commits hasta `bc6b9ff` (2026-07-01, "Dejar de versionar .env"); `git show bc6b9ff^:.env` sigue devolviendo los valores. No hay evidencia de que se hayan rotado después de sacar el archivo del tracking. | historial de git | Rotar `SECRET_KEY`/`ENCRYPTION_KEY`/`DB_PASSWORD` invalida sesiones y datos cifrados existentes — coordinar el corte antes de rotar |

---

## Alto

| # | Hallazgo | Archivo | Nota |
|---|----------|---------|------|
| 3 | `create_subscriber_view` encadena hasta 10 llamadas síncronas a PanAccess dentro de un mismo request; la mitigación (`CREATE_SUBSCRIBER_ASYNC_ENRICHMENT`) existe pero está en `False` tanto de default como en el `.env` real. | `appConfig.py:1086`, `.env` | Puede agotar workers Daphne bajo carga si PanAccess está lento |
| 4 | Sin FK real entre `ListOfSubscriber` y sus dependientes: `delete_subscriber_operational_data` borra `SubscriberLoginInfo`/`SubscriberEmailRegistry`/`SubscriberDocumentRegistry`/`SubscriberInfo` pero **no** toca `UDIDAuthRequest`, `DeviceSession`, `EncryptedCredentialsLog`, `AuthAuditLog`; tampoco corre dentro de `transaction.atomic()`. | `wind/functions/getSubscriber.py:312-349` | Dispositivos huérfanos pueden seguir autenticando tras el borrado |
| 5 | Login (social y manual) sin throttle específico anti fuerza-bruta — solo el anónimo genérico (`AnonBurstThrottle`). Amplifica llamadas a PanAccess bajo un ataque de credential-stuffing. | `wind/auth_views.py` | — |
| 6 | El cambio de password nativo de `dj-rest-auth` no exige `old_password` ni invalida sesiones existentes tras el cambio — mismo problema de fondo que el #1, aplicado a esa vista puntual. | `REST_AUTH` config, `settings.py` | `OLD_PASSWORD_FIELD_ENABLED`/`LOGOUT_ON_PASSWORD_CHANGE` no están seteados |
| 7 | reCAPTCHA totalmente inactivo en los 4 flujos que lo soportan (registro, olvidé contraseña, restablecer contraseña, eliminar cuenta): `RECAPTCHA_SECRET_KEY` vacío en `.env`, y además falta agregar el widget en `register.html`, `forgot-password.html`, `reset-password.html` y el modal de "eliminar cuenta". | `.env`, templates de `wind/templates/wind/` | Backend ya soporta reCAPTCHA, solo falta activarlo |

---

## Medio

| # | Hallazgo | Archivo | Nota |
|---|----------|---------|------|
| 8 | `crypto_tv.py`: el esquema legado (`hybrid_encrypt_for_app`, el que usa el cliente en producción hoy) sigue en AES-CBC sin HMAC/AEAD. El esquema nuevo (`hybrid_encrypt_for_device_public_key`) ya usa AES-GCM — bajó de Alto a Medio porque el riesgo real hoy es solo el camino legado. | `wind/utils/crypto_tv.py` | Migrar cuando el cliente confirme que puede actualizar el lado que descifra |
| 9 | XSS vía `innerHTML` sin escapar en una página de debug: `subscriber_test.html` inserta campos del suscriptor (`comment`, `address`, `firstName`, etc.) directamente en el DOM sin `escapeHtml()`/`textContent`, a diferencia de `dashboard.html` que sí lo hace bien. | `wind/templates/wind/subscriber_test.html:105-150` | Impacto acotado (mayormente self-XSS), pero la página queda expuesta en producción |
| 10 | Filas de telemetría OTT parcialmente corruptas (p. ej. `actionId=8` mal formado) se descartan en silencio, sin `logger.warning`. | `telemetry/services/panaccess_ott_ingest.py:229-230` | Dificulta detectar problemas de datos del lado de PanAccess |
| 11 | `bulk_create(..., ignore_conflicts=True)` en la ingesta de telemetría silencia cualquier violación de constraint, no solo el duplicado esperado por `record_id`. Hoy no hay FK adicional, así que el riesgo es bajo — pero si se agrega una en el futuro, este patrón esconderá pérdida de datos real sin log diferenciado. | `telemetry/services/panaccess_ott_ingest.py:257-259` | A vigilar si el esquema crece |
| 12 | `GET /api/v1/telemetry/top-channels/` solo exige `IsAuthenticated`, sin distinción de rol — no es sensible hoy (ranking global igual para todos), pero no hay barrera si en el futuro se agrega data más granular al mismo endpoint. | `telemetry/views.py:24` | — |
| 13 | El reset/cambio de contraseña no invalida proactivamente los JWT ya emitidos — `SIMPLE_JWT` solo blacklistea un refresh token después de rotarlo. Un access/refresh token robado antes del cambio sigue funcionando hasta que expira por su cuenta. | `password_reset.py`, `profile/views.py`, `change_password.py` | — |
| 14 | `_discover_login_by_login1` puede disparar hasta `PANACCESS_LOGIN_DISCOVERY_MAX_CALLS` (40 por defecto) llamadas síncronas a PanAccess paginando el catálogo completo dentro de un mismo request, si el `login1` numérico no se encuentra localmente. Acotado, pero es una amplificación real solo protegida por el throttle anónimo genérico. | `wind/services/subscriber_auth.py:89-155` | — |
| 15 | WebSockets sin `AllowedHostsOriginValidator`. | Channels/ASGI routing | — |
| 16 | Fingerprint de dispositivo evadible rotando los headers que lo derivan, con comportamiento fail-open. Limitación estructural aceptada, no una decisión nueva. | — | — |
| 17 | Faltan índices funcionales en columnas de búsqueda frecuente: `SubscriberEmailRegistry.email`, `SubscriberLoginInfo.login2`, `ListOfSubscriber.code`. | modelos respectivos | — |
| 18 | `is_reset_token_used`/`mark_reset_token_used` dependen solo de Redis; si Redis falla, hacen fail-open (tratan el token como no usado) — un enlace de reset filtrado se podría reutilizar durante una caída de Redis. | `wind/services/password_reset.py:64-84` | Propuesta: mover el flag también a base de datos |
| 19 | `pre_social_login` confía en el email que devuelve el proveedor social y lo trata como verificado, sin revisar el flag `email_verified` del proveedor, antes de fusionarlo con una cuenta local existente. | `wind/adapters.py:56-65` | — |

---

## Bajo

| # | Hallazgo | Archivo | Nota |
|---|----------|---------|------|
| 20 | Páginas de test/debug (`subscriber-test/`, `login-test/`, `login-test-facebook/`) registradas incondicionalmente en `urls.py`, sin gate de `settings.DEBUG` — quedan accesibles en producción. | `wind/urls.py:45,54,57` | Una de ellas es la del hallazgo #9 de arriba |
| 21 | `dashboard_view` no tiene `login_required` server-side (es intencional: la protección real vive en los endpoints `/api/v1/...` vía JWT) — no es un problema hoy, pero se perdería si algún día se agrega data server-side al contexto de la plantilla. | `wind/views.py:787-789` | Informativo, vigilar a futuro |
| 22 | `event_date` de un evento de telemetría cae en la fecha del servidor si el timestamp original es inválido, en vez de descartarse — puede distorsionar levemente el ranking en días con datos sucios. | `telemetry/services/panaccess_ott_ingest.py:240` | Impacto menor dado el volumen esperado |
| 23 | Race teórica entre cierre de cuenta y sync periódico para un suscriptor que nunca se sincronizó antes — ventana de pocos segundos, ya documentada, no crítica. | `wind/services/subscriber_closure.py` | — |
| 24 | Code smells menores: `attempts_count` de UDID sin usar, orden lexicográfico en `subscriber_code_generator.py`, `client_ip` en WS consumers siempre `127.0.0.1`. | varios | — |
| 25 | Código muerto: subsistema de rate-limit por WebSocket abandonado, 8 serializers sin uso, imports muertos. | varios | Candidato a limpieza |
| 26 | Huecos de cobertura de test en la integración PanAccess, login social, invalidación de JWT y device session. | `wind/tests/` | — |
| 27 | `requirements.txt` está en UTF-16 en vez de UTF-8 (todas las dependencias sí están fijadas con `==`, no es un problema de reproducibilidad) — puede romper linters/herramientas que asuman UTF-8. | `requirements.txt` | Resguardar en UTF-8 |
| 28 | `DeviceSession` no tiene expiración ni limpieza automática — sin decisión tomada todavía. | modelo `DeviceSession` | — |
| 29 | El brand `bromteck` en appVideo sigue apuntando a `http://` en vez de `https://`. | `appVideo/src/config/brands.js` | Pendiente del equipo de appVideo |
| 30 | Router de réplica de base de datos sin failover; falta un timeout de conexión a DB explícito. | `DatabaseConfig` | — |

---

## Posibles mejoras

| # | Mejora | Nota |
|---|--------|------|
| 31 | Definir el site-key de reCAPTCHA para mobile (iOS/Android) en los flujos de "olvidé contraseña" / "eliminar cuenta". | Bloqueante para que el punto 7 se cierre del lado mobile |
| 32 | Desplegar el `.mmdb` de GeoIP en el servidor real de producción (`GEOIP_CITY_DB_PATH`). | — |
| 33 | appVideo: `splashAuthFlow.js` no reabre `ws/device/` tras un refresh de sesión; `loginFlow.js` no espera a que el `device_token` quede persistido antes de continuar. | — |
| 34 | Estandarizar el formato de QR/TV para el emparejamiento de dispositivos. | — |
| 35 | Documentar (o quitar) el campo `name` del JSON de `top-channels`, que hoy no se usa del lado de appVideo (`buildMostWatchedItems` solo cruza por `channel_id`). | No es un bug, solo peso muerto en el contrato |
| 36 | Construir el health endpoint para Zabbix/PRTG que se propuso en la sesión de monitoreo. | — |
| 37 | Dar de alta el worker de systemd para la cola `telemetry` en el servidor de producción (ingest + aggregate no corren sin esto). | Bloqueante para que la app `telemetry` funcione en producción |
| 38 | Completar la Fase 4 de cierre de cuenta (endpoint HTTP admin/autoservicio). | — |
| 39 | Confirmar y corregir el mismatch de paquete Android: `WIND_APP_GOOGLE_PLAY_URL` usa `com.wind.windtv`, mientras que `WINDTV_ANDROID_PACKAGE` (el real, confirmado por el equipo Android) es `com.wind.android.streaming`. | — |
| 40 | Evaluar migrar el esquema legado de `crypto_tv.py` a AEAD cuando el cliente confirme que las apps pueden actualizar el lado que descifra. | Relacionado con el hallazgo #8 |

---

## Resumen numérico

| Categoría | Cantidad |
|-----------|----------|
| Urgente | 2 |
| Alto | 5 |
| Medio | 12 |
| Bajo | 11 |
| Posibles mejoras | 10 |

**Los dos únicos "Urgente" no eran hallazgos nuevos** — eran el mismo problema (rutas nativas de auth) y el mismo riesgo (secretos en git) que ya estaban señalados en `REVISION_INDEPENDIENTE_2026-08-14.md`. El primero (rutas nativas) se resolvió el 2026-08-25 (ver `docs/LIMPIEZA_RUTAS_AUTH_NATIVAS_2026-08-25.md`). El segundo (secretos en el historial de git) sigue abierto -- requiere coordinar la rotación de `SECRET_KEY`/`ENCRYPTION_KEY`/`DB_PASSWORD` antes de ejecutarla.
