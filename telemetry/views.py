from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from telemetry.services.top_channels import (
    DEFAULT_LIMIT,
    DEFAULT_WINDOW_DAYS,
    get_top_channels_global,
)


class TopChannelsGlobalView(APIView):
    """
    GET /api/v1/telemetry/top-channels/  -- ranking global de "canales
    más vistos" (mismo resultado para todos los suscriptores).

    Lee de caché (ver telemetry/services/top_channels.py); no hace
    ningún cálculo pesado en el request en operación normal.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        top_channels = get_top_channels_global(
            window_days=DEFAULT_WINDOW_DAYS, limit=DEFAULT_LIMIT
        )
        return Response(
            {
                "window_days": DEFAULT_WINDOW_DAYS,
                "channels": top_channels,
            }
        )
