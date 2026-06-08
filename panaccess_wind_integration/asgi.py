import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'panaccess_wind_integration.settings')

# Inicializar la aplicación ASGI de Django temprano
django_asgi_app = get_asgi_application()

import wind.routing

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(
            wind.routing.websocket_urlpatterns
        )
    ),
})
