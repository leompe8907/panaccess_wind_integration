import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from wind.api.preferences.serializers import SubscriberPreferencesUpdateSerializer
from wind.models import SubscriberPreferences
from wind.services.subscriber_catalog import resolve_subscriber_code_for_user
from wind.services.subscriber_preferences import get_or_migrate_preferences, serialize_preferences
from wind.throttles import ProfileThrottle

logger = logging.getLogger(__name__)


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
@throttle_classes([ProfileThrottle])
def preferences_view(request):
    """
    GET/PUT /api/v1/preferences/ -- control parental + favoritos
    sincronizados entre dispositivos de una cuenta (ver
    docs/SINCRONIZACION_PREFERENCIAS_2026-08-31.md).

    El `subscriber_code` se resuelve del lado del servidor a partir del
    usuario autenticado (nunca de un valor que mande el cliente, mismo
    patrón que `profile_me_view`/`profile_products_view`). El único dato
    que aporta el cliente es `profileKey` -- el perfil real de PanAccess
    si la cuenta los tiene activados, o se omite/manda vacío para el
    perfil implícito por defecto.

    GET acepta `profileKey` como query param; PUT lo acepta en el body
    (junto con `parental`/`favorites`, ambos opcionales -- solo se
    actualiza lo que venga en el request, no hace falta mandar todo cada
    vez).
    """
    subscriber_code = resolve_subscriber_code_for_user(request.user)
    if not subscriber_code:
        return Response(
            {
                "success": False,
                "message": "No hay suscriptor vinculado a este usuario.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == "GET":
        profile_key = (request.query_params.get("profileKey") or "").strip() or (
            SubscriberPreferences.DEFAULT_PROFILE_KEY
        )
        prefs = get_or_migrate_preferences(subscriber_code, profile_key)
        return Response(serialize_preferences(prefs))

    ser = SubscriberPreferencesUpdateSerializer(data=request.data)
    if not ser.is_valid():
        return Response({"success": False, "errors": ser.errors}, status=status.HTTP_400_BAD_REQUEST)

    profile_key = ser.validated_data.get("profileKey") or SubscriberPreferences.DEFAULT_PROFILE_KEY
    prefs = get_or_migrate_preferences(subscriber_code, profile_key)

    update_fields = []
    if "parental" in ser.validated_data:
        prefs.parental = ser.validated_data["parental"]
        update_fields.append("parental")
    if "favorites" in ser.validated_data:
        prefs.favorite_channel_ids = ser.validated_data["favorites"]
        update_fields.append("favorite_channel_ids")

    if update_fields:
        update_fields.append("updated_at")
        prefs.save(update_fields=update_fields)

    return Response(serialize_preferences(prefs))
