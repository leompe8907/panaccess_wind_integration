"""
Fase 3 -- endpoints REST para listar/revocar "dispositivos vinculados".

Autenticados por JWT (mismo `DEFAULT_AUTHENTICATION_CLASSES` que el resto
de `/api/v1/`, ver settings.py) -- el `subscriber_code` SIEMPRE se resuelve
del lado del servidor a partir del usuario autenticado
(`resolve_subscriber_code_for_user`), nunca se toma de ningún parámetro
que mande el cliente.
"""
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from wind.models import DeviceSession
from wind.services.device_session_service import revoke_device_session
from wind.services.subscriber_catalog import resolve_subscriber_code_for_user


def _serialize_device(session: DeviceSession) -> dict:
    return {
        "id": session.pk,
        "device_type": session.device_type,
        "device_model": session.device_model,
        "first_seen_at": session.first_seen_at,
        "last_seen_at": session.last_seen_at,
        "client_ip": session.client_ip,
        # device_token NUNCA se expone acá -- solo lo conoce el propio
        # dispositivo (lo recibió al registrarse por WebSocket) y el
        # backend; el dashboard revoca por `id`, no por token.
    }


class DeviceSessionListView(APIView):
    """GET -- lista los dispositivos vinculados activos del usuario autenticado."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscriber_code = resolve_subscriber_code_for_user(request.user)
        if not subscriber_code:
            return Response({"devices": []})

        sessions = DeviceSession.objects.filter(
            subscriber_code=subscriber_code, status="active"
        ).order_by("-last_seen_at")

        return Response({"devices": [_serialize_device(s) for s in sessions]})


class DeviceSessionRevokeView(APIView):
    """POST -- revoca un dispositivo vinculado (debe pertenecer al usuario autenticado)."""

    permission_classes = [IsAuthenticated]

    def post(self, request, device_id):
        subscriber_code = resolve_subscriber_code_for_user(request.user)
        if not subscriber_code:
            return Response(
                {"ok": False, "code": "subscriber_unresolved"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = revoke_device_session(subscriber_code=subscriber_code, device_session_id=device_id)
        # El detalle de excepción interna (si lo hay) solo va al log del
        # servidor, nunca al cliente (mismo criterio ya aplicado en Fase 1/2).
        safe_result = {k: v for k, v in result.items() if k != "details"}

        if safe_result.get("ok"):
            return Response(safe_result, status=status.HTTP_200_OK)

        http_status = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "already_revoked": status.HTTP_409_CONFLICT,
            "subscriber_unresolved": status.HTTP_400_BAD_REQUEST,
        }.get(safe_result.get("code"), status.HTTP_400_BAD_REQUEST)
        return Response(safe_result, status=http_status)
