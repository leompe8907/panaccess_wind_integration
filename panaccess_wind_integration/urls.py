from django.contrib import admin
from django.urls import path, include

from wind.views_health import health_view, ready_view

urlpatterns = [
    path('health/', health_view, name='health'),
    path('ready/', ready_view, name='ready'),
    path('admin/', admin.site.urls),
    # URLs nativas de Allauth para renderizado HTML de providers
    path('accounts/', include('allauth.urls')),
    
    # Recuperación de contraseña PanAccess (antes de dj-rest-auth)
    path('api/auth/password/', include('wind.api.password_reset.urls')),

    # Endpoints de JWT y Autenticación REST
    path('api/auth/', include('dj_rest_auth.urls')),
    path('api/auth/registration/', include('dj_rest_auth.registration.urls')),
    path('api/v1/', include('wind.api.urls')),

    path('wind/', include('wind.urls')),
]
