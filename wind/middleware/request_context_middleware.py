"""
Deja la IP del cliente accesible vía contextvar
(`wind.utils.request_context`) durante toda la vida de la request HTTP.

Único consumidor hoy: `applogs.logging_handler.DiagnosticsLogHandler`,
para poder asociar un error de backend (ej. el "Login fallido" que
`wind/utils/panaccess_auth.py` loguea cuando PanAccess rechaza el login
del servicio, varias capas por debajo de cualquier vista) con la IP de
quien disparó esa request -- sin tener que pasar el `request` a mano por
cada función intermedia. Ver docs/IP_ERRORES_BACKEND_2026-09-10.md.

Se instala siempre (no depende de ningún flag): el costo es un
`get_client_ip()` ya optimizado (reutilizado del resto del código) por
request, y no cambia ningún comportamiento existente -- solo deja un
dato disponible para quien lo quiera leer.
"""
from wind.utils.request_context import reset_current_client_ip, set_current_client_ip
from wind.utils.websocket_utils import get_client_ip


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = set_current_client_ip(get_client_ip(request))
        try:
            return self.get_response(request)
        finally:
            reset_current_client_ip(token)
