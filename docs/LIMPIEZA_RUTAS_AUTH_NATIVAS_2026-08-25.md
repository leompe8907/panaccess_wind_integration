# Limpieza de rutas nativas de auth (dj-rest-auth / allauth / registro)

Fecha: 2026-08-25
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Urgente #1
Estado: **Implementado**

## De qué iba el problema

`panaccess_wind_integration/urls.py` montaba tres includes completos de librerías de terceros (`dj_rest_auth.urls`, `dj_rest_auth.registration.urls`, `allauth.urls`) además de los endpoints propios de Wind. Cada uno de esos includes trae de fábrica vistas de reset/cambio de contraseña y de registro que solo conocen el `User` de Django -- no PanAccess, no `wind/services/password_reset.py`, no las tablas de control de trial (`SubscriberEmailRegistry`/`SubscriberDocumentRegistry`).

Con `authenticate_portal_user()` (`wind/services/subscriber_auth.py:340`) probando primero la contraseña local de Django antes de caer a PanAccess, esas rutas nativas no eran solo "duplicado feo": eran una ruta de account-takeover completa que evitaba el throttle (5/hora), el reCAPTCHA, y la sincronización con PanAccess de los endpoints reales. Un password reset hecho por el camino nativo dejaba a la app de TV con una contraseña vieja y al login web con una nueva, sin que nadie lo notara, y sin que el atacante hubiera tocado PanAccess en ningún momento.

En total había **7 endpoints/funciones** que hacían alguna variante de "tocar la contraseña", de los cuales solo 2 estaban construidos a propósito para este proyecto.

## Qué se hizo

### 1. Recortado `dj_rest_auth.urls` a mano

Antes:
```python
path('api/auth/', include('dj_rest_auth.urls')),
```

Ahora (`panaccess_wind_integration/urls.py`), registrando solo las vistas que sí se necesitan, con los mismos nombres de URL que traía la librería (`rest_login`, `rest_logout`, `rest_user_details`, `token_verify`, `token_refresh`) para no romper ninguna referencia interna:

```python
path('api/auth/login/', LoginView.as_view(), name='rest_login'),
path('api/auth/logout/', LogoutView.as_view(), name='rest_logout'),
path('api/auth/user/', UserDetailsView.as_view(), name='rest_user_details'),
path('api/auth/token/verify/', TokenVerifyView.as_view(), name='token_verify'),
path('api/auth/token/refresh/', get_refresh_view().as_view(), name='token_refresh'),
```

Quedan afuera (ya no existen en el proyecto): `password/reset/`, `password/reset/confirm/`, `password/change/`.

`LoginView` sigue usando `PanAccessLoginSerializer` (vía `REST_AUTH['LOGIN_SERIALIZER']`, sin cambios) -- es el mismo login de siempre, solo que ahora está registrado a mano en vez de venir con el paquete completo.

### 2. Quitado `dj_rest_auth.registration.urls` por completo

```python
path('api/auth/registration/', include('dj_rest_auth.registration.urls')),
```

Esta línea se eliminó. El registro real de suscriptores sigue siendo `wind/register/` → `create_subscriber_view` (que sí provisiona en PanAccess y respeta las tablas de control de trial). No hay ningún caso de uso legítimo para un `RegisterView` que cree un `User` de Django suelto.

### 3. `allauth.urls` → solo los callbacks OAuth de Google/Facebook

Antes:
```python
path('accounts/', include('allauth.urls')),
```

`allauth.urls` es la suma de `allauth.account.urls` (login/signup/password reset/change/set en HTML, sin nada de esto adaptado a PanAccess) + los callbacks de cada proveedor social. Ahora se incluyen únicamente los segundos:

```python
from allauth.socialaccount.providers.google.urls import urlpatterns as _google_urlpatterns
from allauth.socialaccount.providers.facebook.urls import urlpatterns as _facebook_urlpatterns
...
path('accounts/', include(_google_urlpatterns + _facebook_urlpatterns)),
```

Esto preserva exactamente `accounts/google/login/callback/` y `accounts/facebook/login/callback/`, que son los que `GOOGLE_REDIRECT_URI`/`FACEBOOK_REDIRECT_URI` del `.env` ya tenían configurados -- no hubo que cambiar nada del lado de Google/Facebook Developer Console.

Antes de aplicar este cambio se confirmó que nada en el código depende de `allauth.account.urls`: no hay ningún `ACCOUNT_ADAPTER` propio (solo `SOCIALACCOUNT_ADAPTER`), ningún template usa `{% url 'account_...' %}`, y la verificación de email de los abonados se marca a mano en código (`mark_portal_email_verified()`/`ensure_subscriber_portal_email_verified()` en `wind/services/subscriber_auth.py`) en vez de depender de que alguien haga clic en el correo de confirmación de allauth.

### 4. Dado de baja `wind/change-password/` (legacy)

Era un cuarto camino para cambiar contraseña, más viejo que `/api/v1/profile/password/` -- el propio docstring de `change_password_view` ya decía *"Preferir: POST /api/v1/profile/password/"*, y la revisión independiente del 14 de agosto confirmó que ningún template interno lo usaba. Se quitó la línea `path('change-password/', ...)` y su import en `wind/urls.py`; la función (`wind/functions/change_password.py`) se dejó intacta, solo dejó de estar montada como ruta.

Se actualizaron los comentarios/documentación que la mencionaban: `wind/services/password_changed_email.py` (docstring) y `README.md` (sección 9.11, marcada como removida).

## Verificación

- `python manage.py check` -- sin errores de import, antes y después de cada cambio.
- Se volcó el `urlconf` completo resuelto (`django.urls.get_resolver()`) y se confirmó:
  - **Siguen existiendo** (sin cambios de contrato): `api/auth/login/`, `api/auth/logout/`, `api/auth/user/`, `api/auth/token/refresh/`, `api/auth/token/verify/`, `api/auth/password/forgot/`, `api/auth/password/reset-confirm/`, `api/v1/profile/password/`, `wind/forgot-password/`, `wind/reset-password/`, `wind/register/`, `accounts/google/login/callback/`, `accounts/facebook/login/callback/`.
  - **Ya no existen**: `api/auth/password/reset/`, `api/auth/password/reset/confirm/`, `api/auth/password/change/`, todo `api/auth/registration/*`, todo `accounts/password/*` (allauth nativo), `wind/change-password/`.

## Impacto esperado en las apps y en lo ya documentado

**Ninguno** en los caminos que las apps ya usan según `GUIA_INTEGRACION_UNIFICADA.md`: ni las URLs, ni los serializers, ni las respuestas de `api/auth/login/`, `api/auth/token/refresh/`, `api/auth/password/forgot/`, `api/auth/password/reset-confirm/` o `/api/v1/profile/password/` cambiaron. El login social (Google/Facebook) tampoco cambia, porque los callbacks que de verdad usa siguen ahí con la misma URL exacta.

Lo único que cambia de comportamiento hacia afuera: las 12 rutas que se quitaron ahora responden **404** en vez de ejecutar una vista insegura. Si algo viejo (un bookmark, un correo archivado, una integración externa que nadie documentó) apuntaba a alguna de ellas, dejará de funcionar -- se revisaron templates, settings y el resto del código en busca de referencias internas y no se encontró ninguna.

## Pendiente relacionado (no cubierto por este cambio)

Quedan como pendientes de una decisión aparte (no forman parte de este cambio): `OLD_PASSWORD_FIELD_ENABLED`/`LOGOUT_ON_PASSWORD_CHANGE` no aplican porque `PasswordChangeView` nativo ya no está montado; el hallazgo Alto "cambio de password no invalida JWT existentes" sigue abierto para `/api/v1/profile/password/` (ver `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, Medio #13); y el resto de hallazgos Urgente/Alto de esa auditoría (secretos en git, throttle de login social/manual, FK huérfanas) no se tocaron en esta sesión.
