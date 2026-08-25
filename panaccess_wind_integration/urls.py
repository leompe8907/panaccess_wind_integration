from django.contrib import admin
from django.urls import path, include
from allauth.socialaccount.providers.google.urls import urlpatterns as _google_urlpatterns
from allauth.socialaccount.providers.facebook.urls import urlpatterns as _facebook_urlpatterns
from dj_rest_auth.views import LoginView, LogoutView, UserDetailsView
from dj_rest_auth.jwt_auth import get_refresh_view
from rest_framework_simplejwt.views import TokenVerifyView

from wind.views_health import health_view, ready_view

urlpatterns = [
    path('health/', health_view, name='health'),
    path('ready/', ready_view, name='ready'),
    path('admin/', admin.site.urls),

    # Solo los callbacks OAuth de Google/Facebook (accounts/google/login/...,
    # accounts/facebook/login/...) -- ver docs/LIMPIEZA_RUTAS_AUTH_NATIVAS_2026-08-25.md.
    # Antes esto era include('allauth.urls') completo, que además montaba
    # allauth.account.urls (login/signup/password reset/change/set nativos
    # de Django, en HTML) sin ninguna de las protecciones/sincronización
    # con PanAccess que sí tiene el resto del proyecto. GOOGLE_REDIRECT_URI/
    # FACEBOOK_REDIRECT_URI del .env siguen apuntando exactamente a las
    # mismas rutas -- no cambia nada del login social.
    path('accounts/', include(_google_urlpatterns + _facebook_urlpatterns)),

    # Recuperación de contraseña PanAccess (antes de dj-rest-auth)
    path('api/auth/password/', include('wind.api.password_reset.urls')),

    # Endpoints de JWT y Autenticación REST -- recortado a mano (antes
    # include('dj_rest_auth.urls') completo, que también montaba
    # password/reset/, password/reset/confirm/ y password/change/ nativos:
    # solo tocan el User de Django, sin throttle real ni sync con
    # PanAccess, duplicando lo que ya hacen bien wind.api.password_reset.urls
    # (arriba) y /api/v1/profile/password/. Se dejan solo login/logout/
    # user/token-refresh/token-verify, que son los que de verdad usa el
    # device-session de appVideo (ver GUIA_INTEGRACION_UNIFICADA.md).
    # Tampoco se incluye dj_rest_auth.registration.urls -- el registro real
    # es wind/register/ (create_subscriber_view), no un User de Django suelto.
    # Ver docs/LIMPIEZA_RUTAS_AUTH_NATIVAS_2026-08-25.md.
    path('api/auth/login/', LoginView.as_view(), name='rest_login'),
    path('api/auth/logout/', LogoutView.as_view(), name='rest_logout'),
    path('api/auth/user/', UserDetailsView.as_view(), name='rest_user_details'),
    path('api/auth/token/verify/', TokenVerifyView.as_view(), name='token_verify'),
    path('api/auth/token/refresh/', get_refresh_view().as_view(), name='token_refresh'),

    path('api/v1/', include('wind.api.urls')),
    path('api/v1/telemetry/', include('telemetry.urls')),

    path('wind/', include('wind.urls')),
]
