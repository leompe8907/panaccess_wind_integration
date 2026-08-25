from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from telemetry.services.top_channels import (
    DEFAULT_LIMIT,
    DEFAULT_WINDOW_DAYS,
    build_top_channels_bouquet,
)


class TopChannelsGlobalView(APIView):
    """
    GET /api/v1/telemetry/top-channels/  -- ranking global de "canales
    más vistos" (mismo resultado para todos los suscriptores), entregado
    con la misma forma que un bouquet real de PanAccess (bouquetId, name,
    customData) para que appVideo lo renderice con el mismo componente que
    usa para bouquets reales.

    Lee de caché (ver telemetry/services/top_channels.py); no hace
    ningún cálculo pesado en el request en operación normal.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            build_top_channels_bouquet(window_days=DEFAULT_WINDOW_DAYS, limit=DEFAULT_LIMIT)
        )
