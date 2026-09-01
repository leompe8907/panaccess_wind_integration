# Sincronización de control parental y favoritos entre dispositivos

Fecha: 2026-08-31

## Qué

Nueva funcionalidad (no es un hallazgo de auditoría): control parental (PIN propio de la app, no el de la tarjeta/PanAccess) y canales favoritos, antes guardados solo en `localStorage` por dispositivo, ahora se sincronizan entre todos los dispositivos de una misma cuenta. Alcance: solo brand `wind` -- ver "Por qué" para el motivo de esa limitación.

### Backend (Back-Wind-V2)

- `wind/models.py`: modelo nuevo `SubscriberPreferences` -- `subscriber_code` + `profile_key` (único juntos), `parental` (JSON), `favorite_channel_ids` (JSON), timestamps. `profile_key` usa el sentinel `"default"` (`SubscriberPreferences.DEFAULT_PROFILE_KEY`) cuando la cuenta no tiene perfiles PanAccess activos o todavía no se eligió ninguno.
- `wind/migrations/0010_subscriber_preferences.py`.
- `wind/services/subscriber_preferences.py`: `get_or_migrate_preferences(subscriber_code, profile_key)` -- trae la fila si existe; si no, y es la primera vez que se usa un perfil real (no `"default"`) para esa cuenta, copia una sola vez el contenido de la fila `"default"` hacia el perfil nuevo (creación con `IntegrityError` capturado, para no fallar ante una carrera entre dos dispositivos creando la misma fila a la vez). Perfiles reales siguientes arrancan vacíos. `serialize_preferences(prefs)` arma la respuesta JSON.
- `wind/api/preferences/` (serializers.py, views.py, urls.py -- mismo patrón que `wind/api/profile/`, sin `__init__.py`, namespace package implícito): `GET/PUT /api/v1/preferences/`, `@permission_classes([IsAuthenticated])`, `@throttle_classes([ProfileThrottle])`. El `subscriber_code` nunca se toma del cliente -- se resuelve del usuario autenticado vía `resolve_subscriber_code_for_user(request.user)`, mismo patrón que el resto de `wind/api/`.
  - `GET ?profileKey=...` (opcional, default `"default"`): devuelve `{success, profileKey, parental, favorites}`.
  - `PUT {profileKey?, parental?, favorites?}`: actualiza solo los campos presentes (actualización parcial -- mandar solo `favorites` no borra `parental`, y viceversa). `parental` debe ser un objeto (rechaza si no), máx. ~20KB. `favorites` es una lista de strings, máx. 500 elementos.
- `wind/api/urls.py`: `path("preferences/", include("wind.api.preferences.urls"))`, entre `profile/` y `tasks/`.
- `wind/tests/test_subscriber_preferences.py`: 13 tests (`PreferencesViewTestCase` + `GetOrMigratePreferencesTestCase`), corridos contra Postgres real en sandbox -- todos verdes.
- Sin variables nuevas en `appConfig.py`/`.env` -- esta funcionalidad no tiene parámetros configurables, reutiliza `ProfileThrottle` ya existente.

### Frontend (appVideo)

- `src/services/preferencesSyncService.js` (nuevo): `pullPreferences()` (GET), `pushPreferences(partial)` (PUT, fire-and-forget), `syncPreferencesFromBackend()` (pull + aplica a los dos stores locales). `resolveProfileKey()` lee el perfil PanAccess activo (`useActiveProfileStore`) o cae al mismo sentinel `"default"` que usa el backend. Todo el módulo usa `hasDeviceSessionAuth(brand)` como guardia de entrada y nunca lanza hacia el caller -- si no hay sesión de dispositivo (brand sin `login.deviceSession.enabled`, hoy solo `wind`) o falla la red, no hace nada y `localStorage` sigue siendo la fuente de verdad local, igual que antes de esta funcionalidad. `syncPreferencesFromBackend()` usa `import()` dinámico hacia `parentalStore.js`/`userPreferences.js` a propósito, para no crear un ciclo de imports estáticos (ambos importan este archivo para el lado del push).
- `src/utils/userPreferences.js`: `setFavorites(brandId, favorites, {skipSync=false}={})` ahora empuja al backend tras guardar en `localStorage`, salvo que `skipSync: true` (usado cuando el valor recién llegó del backend, para no reenviarlo de inmediato). `toggleFavorite` hereda el push automáticamente porque llama a `setFavorites` internamente.
- `src/store/parentalStore.js`: el `persist` interno se separó en `persistLocalOnly` (siempre, incluye estado transitorio de desbloqueo) y `buildDurableParentalPayload` (solo la config "durable": `enabled`, PIN, canales bloqueados, config de clasificación -- explícitamente **sin** `unlockUntilMs`/`lastUnlockScope`/`lastUnlockedChannelId`/`ratingUnlockUntilMs`, ver "Por qué"). `persist()` ahora hace ambas cosas: guarda local y empuja `{parental: buildDurableParentalPayload(s)}` al backend. Acción nueva `hydrateFromRemote(remote)`: aplica una config remota al store y persiste solo localmente (no reenvía).
- `src/hooks/useAppLifecycle.js`: `syncPreferencesFromBackend()` se llama junto a `ensureDeviceSessionConnected(currentBrand)` en los dos mismos puntos que ese watchdog ya usaba -- al montar/cambiar de brand (con el mismo `setTimeout` de 1500ms) y en `handleShow` (retorno de background). Mismo criterio: fire-and-forget, no bloquea nada.

## Por qué

**Separación estado durable vs. transitorio (parentalStore):** un desbloqueo temporal de control parental en un dispositivo (p. ej. "desbloqueado por 30 minutos" en la TV del salón) nunca debe sincronizarse -- si lo hiciera, desbloquearía silenciosamente el control parental en el teléfono de otra persona de la casa. Por eso `buildDurableParentalPayload` excluye explícitamente todos los campos de desbloqueo/sesión; solo se sincroniza la configuración (PIN, lista de bloqueados, clasificación), nunca el estado de "ya lo desbloqueé".

**Sentinel `"default"` + migración automática:** hoy ninguna app tiene perfiles PanAccess activos ni canales configurados como favoritos/parental -- el diseño tenía que funcionar igual de bien para una cuenta sin perfiles que para una con varios. Con `profile_key="default"` como piso, todo funciona desde el día uno sin perfiles. Cuando una cuenta activa perfiles por primera vez, el primer perfil real hereda una vez lo que había en `"default"` (para no perder la config que el usuario ya tenía), y perfiles siguientes arrancan limpios (cada persona de la casa configura lo suyo). Importante: el PIN de perfil de PanAccess (que viene de la smartcard, se lee automático vía `getStreamingLicenses({withPins:true})` y el usuario nunca escribe) no tiene nada que ver con esto -- esta sincronización es exclusivamente del PIN propio de la app (`parentalStore.js`), un sistema completamente aparte.

**Alcance limitado a `wind`:** la única forma de autenticar una llamada a `/api/v1/` en appVideo es el JWT de "dispositivo vinculado" (`deviceAuthService.js`), que solo se obtiene cuando `login.deviceSession.enabled` está activo en `brands.js` -- hoy solo cierto para `wind`. No existe un JWT "base" independiente de ese sistema para las demás marcas. Como Back-Wind-V2 se construyó inicialmente para Wind y las demás marcas son clientes independientes, se decidió no forzar unificación de auth entre marcas; si a futuro otro cliente quiere esta misma funcionalidad, la lógica de este backend (modelo, servicio, endpoint) se puede copiar para levantar uno propio.

**Nunca rompe el uso local:** todo el lado de sync (pull y push) está diseñado para fallar en silencio -- sin sesión de dispositivo, sin red, o con el backend caído, la app sigue funcionando exactamente como antes de esta funcionalidad (favoritos/parental solo local). Esto sigue el mismo patrón ya establecido en `mostWatchedChannelsService.js`.

## Cómo se verificó

- Backend: `python3 -m py_compile` limpio sobre los archivos nuevos/tocados; `python3 manage.py check` -- `System check identified no issues (0 silenced)`; `wind/tests/test_subscriber_preferences.py` (13 tests) corridos contra Postgres real en sandbox (`pgserver`) -- `Ran 13 tests ... OK`. Cubren: creación de fila default en el primer GET, 404 si el usuario no tiene `subscriber_code` vinculado, PUT actualiza ambos campos, PUT parcial no borra el otro campo, un GET lee lo que puso otro "dispositivo" (otro PUT), `profileKey` en blanco cae a `"default"`, perfiles distintos quedan aislados y la migración no pisa hacia atrás la fila `"default"` original, validación rechaza `parental` que no es objeto y `favorites` con más de 500 elementos; y por separado, la lógica de migración automática (`get_or_migrate_preferences`): primer perfil real hereda de `"default"`, segundo perfil real arranca vacío, cuenta sin fila `"default"` no falla, fila ya existente se devuelve sin tocar la migración.
- Frontend: verificación de sintaxis con el parser de Babel (`@babel/parser`, `sourceType: 'module'`, plugin `jsx`) sobre los 4 archivos nuevos/tocados (`preferencesSyncService.js`, `userPreferences.js`, `parentalStore.js`, `useAppLifecycle.js`) -- los 4 parsean sin errores.
- No se corrió un smoke test end-to-end en dispositivo real (fuera del alcance del sandbox); queda pendiente que el usuario lo pruebe en dos dispositivos reales con la marca `wind` antes de considerar esto cerrado en producción.

## Archivos tocados

Backend (Back-Wind-V2):
- `wind/models.py` (`SubscriberPreferences`)
- `wind/migrations/0010_subscriber_preferences.py` (nuevo)
- `wind/services/subscriber_preferences.py` (nuevo)
- `wind/api/preferences/serializers.py`, `views.py`, `urls.py` (nuevos)
- `wind/api/urls.py` (registra `preferences/`)
- `wind/tests/test_subscriber_preferences.py` (nuevo, 13 tests)

Frontend (appVideo):
- `src/services/preferencesSyncService.js` (nuevo)
- `src/utils/userPreferences.js` (`setFavorites` con push + `skipSync`)
- `src/store/parentalStore.js` (`persistLocalOnly`/`buildDurableParentalPayload`/`persist`, acción `hydrateFromRemote`)
- `src/hooks/useAppLifecycle.js` (llamada a `syncPreferencesFromBackend()` en los dos puntos donde ya vive `ensureDeviceSessionConnected`)

No aplica: no hay fila nueva en `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` -- esto es una funcionalidad nueva, no un hallazgo de auditoría. No hay variables nuevas en `appConfig.py`/`.env`.
