import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'panaccess_wind_integration.settings')

# Inicializar la aplicación ASGI de Django temprano
django_asgi_app = get_asgi_application()

import wind.routing
from wind.utils.ws_origin_validator import native_aware_origin_validator

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    # Validador de Origin (auditoría, Medio #15) envolviendo todo lo demás
    # -- rechaza un Origin web no autorizado antes de gastar nada en auth;
    # ver wind/utils/ws_origin_validator.py sobre por qué no se usa
    # AllowedHostsOriginValidator de Channels tal cual (rompería clientes
    # nativos sin header Origin).
    "websocket": native_aware_origin_validator(
        AuthMiddlewareStack(
            URLRouter(
                wind.routing.websocket_urlpatterns
            )
        )
    ),
})
