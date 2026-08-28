"""
Validador de `Origin` para WebSockets (auditoría, Medio #15).

`channels.security.websocket.AllowedHostsOriginValidator` no sirve tal
cual acá: usa `settings.ALLOWED_HOSTS` (hosts concretos, sin `"*"`) como
lista de orígenes permitidos, y el `OriginValidator` de Channels en el
que se apoya **rechaza cualquier conexión sin header `Origin`** a menos
que `"*"` esté en esa lista (ver `valid_origin()` en
`channels/security/websocket.py`). El problema: los clientes nativos
(apps Android/iOS, Smart TV con librería WebSocket nativa) normalmente
no mandan header `Origin` en absoluto -- es un concepto de navegador.
Aplicar el validador de Channels tal cual cortaría en producción el
pareo de TV (`/ws/auth/`) y "dispositivos vinculados" (`/ws/device/`)
para cualquier cliente que no sea un navegador, sin que nadie lo pida.

`NativeAwareOriginValidator` resuelve esto:
  - Si la conexión NO trae header `Origin`, se permite (clientes
    nativos -- no hay nada que validar).
  - Si SÍ trae `Origin`, se exige que esté en la lista de orígenes web
    ya autorizados (se reutiliza `settings.CORS_ALLOWED_ORIGINS` -- la
    misma lista que ya decide qué webs pueden llamar la API REST vía
    CORS, en vez de inventar una lista nueva para mantener en paralelo).

Esto bloquea el escenario real que motiva este hallazgo -- una página
web maliciosa corriendo en el navegador de la víctima que intenta abrir
un WebSocket contra este backend -- sin afectar a los clientes nativos.
"""
from channels.security.websocket import OriginValidator
from django.conf import settings


class NativeAwareOriginValidator(OriginValidator):
    def valid_origin(self, parsed_origin):
        # A diferencia de OriginValidator.valid_origin (que rechaza
        # `None` salvo que "*" esté en la lista), acá la ausencia de
        # `Origin` es el caso normal de un cliente nativo -- se permite
        # directo, sin pasar por validate_origin().
        if parsed_origin is None:
            return True
        return self.validate_origin(parsed_origin)


def native_aware_origin_validator(application):
    """Factory, mismo estilo que `AllowedHostsOriginValidator` de Channels."""
    return NativeAwareOriginValidator(application, list(settings.CORS_ALLOWED_ORIGINS))
