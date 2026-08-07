"""
Manejador de excepciones global de DRF.

Reemplaza el mensaje por defecto de `Throttled` ("Request was throttled...",
con la palabra "throttled" sin traducir filtrándose al usuario final) por uno
en español, en el mismo formato {success, error_type, message} que ya usa el
resto de la API, y con el tiempo de espera expresado en minutos en vez de
segundos crudos.
"""
from rest_framework.exceptions import Throttled
from rest_framework.views import exception_handler as drf_exception_handler


def _formatear_espera(segundos: int) -> str:
    minutos = max(1, round(segundos / 60))
    if minutos == 1:
        return "1 minuto"
    return f"{minutos} minutos"


def custom_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)

    if isinstance(exc, Throttled) and response is not None:
        espera = _formatear_espera(exc.wait or 0)
        response.data = {
            "success": False,
            "error_type": "Throttled",
            "message": (
                f"Has hecho demasiados intentos. Por favor espera {espera} "
                "antes de volver a intentarlo."
            ),
        }

    return response
