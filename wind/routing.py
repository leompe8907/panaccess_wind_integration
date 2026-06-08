from django.urls import re_path
from .consumers import AuthWaitWS

websocket_urlpatterns = [
    re_path(r"^ws/auth/$", AuthWaitWS.as_asgi()),
]
