from django.urls import re_path
from .consumers import AuthWaitWS
from .device_consumers import DeviceSessionWS

websocket_urlpatterns = [
    re_path(r"^ws/auth/$", AuthWaitWS.as_asgi()),
    # Fase 3 -- registro de "dispositivos vinculados" (ver device_consumers.py).
    re_path(r"^ws/device/$", DeviceSessionWS.as_asgi()),
]
