# Logs de diagnóstico para desarrolladores (`applogs`)

Fecha: 2026-09-01

## Qué

Funcionalidad nueva (no es un hallazgo de auditoría): un sistema propio, self-hosted, para que el equipo revise errores de las apps (appVideo web/TV, y a futuro iOS/Android) y del propio backend sin depender de que el suscriptor lo reporte manualmente. No es telemetría de negocio (eso vive en la app `telemetry`, consumo/reproducción) ni auditoría de seguridad (eso vive en `wind.models.AuthAuditLog`/`wind.utils.log_buffer`) -- es diagnóstico técnico puro.

Se construyó como app propia (`applogs`) en vez de sumarla a `wind` (que ya concentra 17 modelos en un solo `models.py`) para no seguir apilando dominios sin relación en el mismo lugar -- primer paso concreto hacia separar el proyecto en apps por dominio ("monolito modular") en vez de seguir creciendo `wind` orgánicamente.

Se evaluó adoptar una herramienta ya hecha para esto (Sentry u otra) en vez de construir propio -- decisión explícita del cliente: sistema propio, self-hosted, sin depender de un tercero ni sacar datos de suscriptores fuera de la infraestructura propia. (Nota aparte: el proyecto ya tenía `SentryConfig`/`sentry_sdk.init()` scaffoleado en `settings.py`, gateado por `SENTRY_DSN` -- vacío hoy, así que dormido. Cubre solo el backend Django si algún día se activa; no reemplaza esto, que además cubre appVideo/iOS/Android.)

### Modelo de datos: agrupado, no un log plano

Dos tablas, no una -- mismo criterio que herramientas tipo Sentry: agrupar por "issue" (el mismo error repetido), no una fila suelta por cada ocurrencia sin relación entre sí.

- `LogIssue`: `fingerprint` (hash de plataforma+nivel+mensaje+primera línea del stack -- agrupa el mismo error repetido), `platform`, `level`, `message` corto, `status` (abierto/resuelto/ignorado), `occurrence_count`, `first_seen_at`, `last_seen_at`, `last_alerted_at`.
- `LogEvent`: FK a `LogIssue`, `subscriber_code` (opcional -- nunca se confía como identidad, es solo contexto para filtrar; puede quedar vacío si el error ocurrió antes del login), `device_type`, `app_version`, `stack` completo, `breadcrumbs` (JSON), `extra` (JSON), `client_ip`, `created_at`.

### Ingesta HTTP: `POST /api/v1/logs/` (`applogs/views.py`)

Pensado para appVideo/iOS/Android -- contrato completo documentado en `docs/GUIA_INTEGRACION_UNIFICADA.md` (nueva sección). Puntos clave de la vista:

- **Acepta requests sin JWT a propósito** -- un crash puede pasar antes del login (ej. pantalla de login rota). Autenticación manual y best-effort (`_resolve_subscriber_code_best_effort`): si viene un `Authorization: Bearer <jwt>` válido, resuelve `subscriber_code` igual que el resto de `/api/v1/`; si no viene, viene vencido, o es inválido, el request sigue igual, solo que sin ese dato de contexto. Nunca rechaza por un JWT malo.
- **Como acepta requests sin JWT, necesita otro candado**: header `X-App-Log-Key` comparado en tiempo constante (`hmac.compare_digest`) contra `AppLogsConfig.INGEST_API_KEY`. Si esa variable no está configurada en `.env`, el endpoint **rechaza todo** (falla cerrado, no abierto) -- hay que generar un valor real antes de producción.
- Rate limit propio (`LogIngestThrottle`, scope `log_ingest`, default 30/minute) -- ver `wind/throttles.py`, mismo patrón que el resto de límites del proyecto.
- Validación (`LogEventIngestSerializer`): `platform` debe ser uno de los choices conocidos, `message` obligatorio, `stack` hasta 8000 caracteres, `breadcrumbs` hasta 100 entradas (cada una un objeto), `extra` hasta ~20KB serializado.

### Logs del propio backend (`applogs/logging_handler.py`)

`DiagnosticsLogHandler`, wireado en `LOGGING` (settings.py) exactamente en los mismos loggers donde ya está `error_file` (root, django, django.request, wind, wind.services.panaccess_*, wind.apps, celery/celery.worker/celery.beat) -- duplica hacia esta base de datos todo lo que ya se escribía en `errors.log`, sin tocar ese archivo ni su comportamiento.

Reglas duras porque corre dentro del logging normal de cualquier request/tarea: `emit()` nunca deja escapar una excepción (atrapa todo y loguea a un logger interno que nunca tiene este mismo handler adjunto, para no recursar), y respeta un kill switch propio (`APP_LOGS_BACKEND_CAPTURE_ENABLED`) independiente del resto de `applogs` por si hiciera falta apagar solo esta parte.

### Migración automática (auto-agrupación) y alertas

`record_log_event()` (`applogs/services.py`) es el único punto de entrada, usado tanto por la vista HTTP como por el handler de logging -- así ambos caminos agrupan y alertan exactamente igual. Calcula el `fingerprint`, hace `get_or_create` del `LogIssue` (con manejo de `IntegrityError` para la carrera de dos requests creando el mismo fingerprint a la vez, mismo patrón que `wind.services.subscriber_preferences.get_or_migrate_preferences`), incrementa `occurrence_count` de forma atómica (`F("occurrence_count") + 1`), crea el `LogEvent`, y dispara una alerta si corresponde.

Alerta por email (`EmailConfig` ya estaba configurado en el proyecto, se reutiliza tal cual): se manda cuando aparece un `LogIssue` **nuevo**, o cuando uno ya conocido cruza un múltiplo de `APP_LOGS_ALERT_SPIKE_EVERY` ocurrencias (default 50 -- para detectar que un error "ya resuelto" volvió con fuerza). Cooldown por issue (`APP_LOGS_ALERT_COOLDOWN_MINUTES`, default 60) para no saturar el correo con el mismo error repitiéndose. Nunca rompe la ingesta si el envío falla (try/except silencioso con log de warning).

### Panel de consulta: Django admin

`applogs/admin.py` -- `wind` (la app de negocio principal) no usa Django admin para nada, pero `telemetry` sí, y este panel es interno para desarrolladores, no para suscriptores, así que admin es la opción más rápida de tener andando. Lista `LogIssue` (no eventos sueltos) ordenado por `last_seen_at`, con filtros por plataforma/nivel/status, búsqueda por mensaje/fingerprint, acciones en lote para marcar resuelto/ignorado/reabrir, y los `LogEvent` de cada issue como inline.

### Retención

`applogs/tasks.py:purge_old_log_events_task` -- mismo patrón que `wind.tasks.check_replica_health_task` (Celery Beat, `shared_task(bind=True)`, guardado por su propia bandera de config). Borra solo `LogEvent` más viejos que `APP_LOGS_RETENTION_DAYS` (default 90) -- el `LogIssue` agregado **nunca se borra acá**, queda como historial liviano ("este error existió, se vio N veces entre tal fecha y tal otra") aunque se pode el detalle de cada ocurrencia individual. Registrada en `CELERY_BEAT_SCHEDULE["purge-old-log-events"]`, condicionada a `APP_LOGS_RETENTION_ENABLED`.

## Por qué

**Agrupado por issue, no un log plano:** 500 ocurrencias del mismo error de red no deben verse como 500 filas sin relación en el panel -- agrupar por `fingerprint` es lo que hace que el panel sea usable para "¿qué errores nuevos aparecieron?" en vez de un volcado de texto.

**Sin JWT obligatorio, pero con API key:** el caso de uso central es justamente el que el suscriptor no reporte nada -- eso incluye errores que pasan antes de loguearse (pantalla de login rota, por ejemplo). Exigir JWT haría que el sistema fallara exactamente en el caso donde más se lo necesita. La API key es el candado que reemplaza al JWT para que esto no sea un endpoint de escritura abierto a cualquiera.

**Separación durable (`LogIssue`) vs. detalle (`LogEvent`) con retención distinta:** permite podar el volumen (stacks, breadcrumbs, contexto de dispositivo -- lo pesado) sin perder el historial agregado (cuántas veces pasó, cuándo empezó, cuándo fue la última vez) que es lo que en la práctica se usa para priorizar qué arreglar primero.

**App propia en vez de sumarla a `wind`:** evita seguir acumulando dominios sin relación en el archivo de modelos más grande del repo, y deja un precedente concreto (ya hay dos apps además de `wind`: `telemetry` y ahora `applogs`) para separar el resto del proyecto en apps por dominio más adelante, sin que eso implique tocar la infraestructura de despliegue (sigue siendo un solo proyecto Django, un solo servidor).

## Cómo se verificó

- `applogs/*.py` y `applogs/migrations/0001_initial.py`: `python3 -m py_compile` y `python3 -m pyflakes` limpios (0 errores/avisos).
- Revisión manual completa de cada archivo contra el repo real (releído tal cual quedó en disco, no solo lo que se escribió).
- `applogs/tests.py`: 20 tests escritos cubriendo ingesta (creación de issue/event, agrupación de errores repetidos por fingerprint, plataformas distintas generan issues distintos, rechazo de API key ausente/incorrecta, validaciones de payload, resolución de `subscriber_code` desde un JWT válido, un JWT ausente/inválido no bloquea el reporte), el servicio (`compute_fingerprint` estable y sensible a la plataforma, alerta por email en issue nuevo, no re-alerta dentro del cooldown, respeta `ALERTS_ENABLED`/destinatarios vacíos) y la tarea de retención (borra solo lo viejo, nunca borra el `LogIssue`, respeta el flag de activación).
- **Pendiente, bloqueado por el entorno:** no se pudo correr `manage.py check` ni los tests contra Postgres real (`pgserver`) en esta sesión -- el sandbox de shell perdió el montaje de este repo a mitad de la implementación (ver aviso al usuario en el chat) y no se pudo restablecer sin un reinicio de la aplicación. Apenas se recupere el acceso, correr: `manage.py makemigrations --check` (confirmar que la migración escrita a mano en `0001_initial.py` coincide exactamente con lo que Django generaría de los modelos), `manage.py check`, y la suite completa de `applogs/tests.py` contra Postgres real.

## Archivos tocados

Nuevos (`applogs/`, app completa): `__init__.py`, `apps.py`, `models.py`, `migrations/__init__.py`, `migrations/0001_initial.py`, `services.py`, `serializers.py`, `views.py`, `urls.py`, `admin.py`, `tasks.py`, `logging_handler.py`, `tests.py`.

Modificados:
- `appConfig.py` -- `ThrottleConfig.LOG_INGEST`, nueva clase `AppLogsConfig` completa.
- `panaccess_wind_integration/settings.py` -- `INSTALLED_APPS` (`applogs`), `DEFAULT_THROTTLE_RATES` (`log_ingest`), `LOGGING` (handler `diagnostics` + wireado en los mismos loggers que `error_file`), `CELERY_BEAT_SCHEDULE` (`purge-old-log-events`), import de `AppLogsConfig`.
- `wind/throttles.py` -- `LogIngestThrottle`.
- `wind/api/urls.py` -- `path("logs/", include("applogs.urls"))`.
- `.env` -- `DRF_THROTTLE_LOG_INGEST`, `APP_LOGS_INGEST_KEY`, `APP_LOGS_ALERTS_ENABLED`, `APP_LOGS_ALERT_RECIPIENTS`, `APP_LOGS_ALERT_SPIKE_EVERY`, `APP_LOGS_ALERT_COOLDOWN_MINUTES`, `APP_LOGS_RETENTION_ENABLED`, `APP_LOGS_RETENTION_DAYS`, `APP_LOGS_RETENTION_MINUTES`, `APP_LOGS_BACKEND_CAPTURE_ENABLED`.
- `docs/GUIA_INTEGRACION_UNIFICADA.md` -- nueva sección con el contrato de ingesta para appVideo/iOS/Android.

No aplica: no hay fila nueva en `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` -- esto es una funcionalidad nueva, no un hallazgo de auditoría.

## Pendiente antes de producción

- Generar un valor real para `APP_LOGS_INGEST_KEY` en el `.env` del servidor (hoy vacío en el ejemplo -- el endpoint rechaza todo hasta que se configure).
- Definir `APP_LOGS_ALERT_RECIPIENTS` con las direcciones del equipo que deben recibir las alertas.
- Correr `manage.py makemigrations --check` + `manage.py check` + la suite de tests contra Postgres real apenas se recupere el acceso de shell a este repo (ver "Cómo se verificó").
- Wiring del lado de appVideo (breadcrumbs + llamada a este endpoint desde `errorReporting.js`) -- todavía no implementado, queda como siguiente paso.
